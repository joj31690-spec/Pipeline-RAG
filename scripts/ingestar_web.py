"""Pipeline concurrente de ingesta web multi-tenant con aislamiento por coleccion_id.

Requerimiento 2 de la practica: descarga HTTP y limpieza de HTML desacopladas con
ThreadPoolExecutor, particionamiento de texto con ProcessPoolExecutor, embeddings por
lotes con control de rate limit (429 / RESOURCE_EXHAUSTED) e insercion masiva.
"""
import argparse
import json
import os
import random
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types
from supabase import create_client

load_dotenv()

MODEL_EMBEDDING = "models/gemini-embedding-001"
BATCH_SIZE = 10
PAUSA_ENTRE_LOTES = 1.0
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (RAG-lexbancario/1.0)"
ETIQUETAS_IRRELEVANTES = {"script", "style", "nav", "header", "footer", "noscript", "aside", "form"}


@dataclass
class Metricas:
    fases: dict = field(default_factory=lambda: defaultdict(float))
    url_completadas: int = 0
    url_fallidas: int = 0
    urls_duplicadas: int = 0
    fragmentos: int = 0
    fragmentos_sin_embedding: int = 0
    inserted: int = 0
    cpu_descarga: float = 0.0
    cpu_chunking: float = 0.0

    @contextmanager
    def medir(self, fase):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.fases[fase] += time.perf_counter() - t0

    def desglose_pct(self) -> dict:
        total = sum(self.fases.values()) or 1e-9
        return {k: round(100.0 * v / total, 2) for k, v in self.fases.items()}

    def registrar_fase(self, fase: str, segundos: float) -> None:
        """Registra una fase no ejecutada como 0 para que el desglose % sume 100."""
        self.fases[fase] = segundos


def limpiar_html(html: str) -> str:
    sopa = BeautifulSoup(html, "lxml")
    for etiqueta in ETIQUETAS_IRRELEVANTES:
        for nodo in sopa.find_all(etiqueta):
            nodo.decompose()
    texto = sopa.get_text(separator="\n")
    lineas = [ln.strip() for ln in texto.splitlines()]
    return "\n".join(ln for ln in lineas if len(ln) > 2)


def descargar_una(url: str, timeout: float, reintentos: int) -> dict:
    ultimo_error = None
    for intento in range(reintentos + 1):
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
                verify=True,
            ) as cliente:
                r = cliente.get(url)
            if r.status_code == 429:
                espera = min(2 ** intento + random.uniform(0, 1), 30)
                time.sleep(espera)
                ultimo_error = f"HTTP 429 (rate limit del sitio)"
                continue
            r.raise_for_status()
            return {
                "url": url,
                "ok": True,
                "html": r.text,
                "bytes": len(r.content),
            }
        except Exception as exc:
            ultimo_error = f"{type(exc).__name__}: {exc}"
            time.sleep(min(2 ** intento, 5))
    return {"url": url, "ok": False, "error": ultimo_error, "html": "", "bytes": 0}


def trocear_texto(texto: str, chunk_size: int, chunk_overlap: int) -> list:
    """Particionado deslizante. Funcion top-level para ProcessPoolExecutor."""
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap debe ser menor que chunk_size")
    bloques = []
    inicio = 0
    largo = len(texto)
    while inicio < largo:
        fin = min(inicio + chunk_size, largo)
        bloque = texto[inicio:fin].strip()
        if len(bloque) > 40:
            bloques.append(bloque)
        if fin == largo:
            break
        inicio += (chunk_size - chunk_overlap)
    return bloques


def limpiar_y_trocear(html: str, chunk_size: int, chunk_overlap: int) -> list:
    """Limpieza HTML + particionado en un solo task de CPU.

    Debe ser top-level para ProcessPoolExecutor: la limpieza (BeautifulSoup)
    es trabajo de CPU, asi que debe ejecutarse DENTRO del proceso-trabajador.
    Si se limpiara en el hilo principal, la fase 'chunking' mediria trabajo
    secuencial y el speedup de la fase seria artificialmente malo.
    """
    return trocear_texto(limpiar_html(html), chunk_size, chunk_overlap)


def generar_embeddings(ai_client, textos: list, reintentos_max: int = 5) -> list:
    espera = 2.0
    for intento in range(reintentos_max):
        try:
            res = ai_client.models.embed_content(
                model=MODEL_EMBEDDING,
                contents=textos,
                config=types.EmbedContentConfig(output_dimensionality=768),
            )
            return [e.values for e in res.embeddings]
        except Exception as exc:
            mensaje = str(exc)
            transitorio = ("429" in mensaje or "RESOURCE_EXHAUSTED" in mensaje
                           or "503" in mensaje or "UNAVAILABLE" in mensaje)
            if not transitorio or intento == reintentos_max - 1:
                raise
            time.sleep(espera)
            espera = min(espera * 2, 60.0)
    raise RuntimeError("sin embeddings tras reintentos")


def insertar_lote(supabase, filas: list, reintentos_max: int = 3) -> int:
    """Inserta un lote de filas con reintentos exponenciales.

    Sin esto, un corte de red momentaneo con Supabase descartaba el lote entero
    de forma irrecuperable, ya que los vectores de ese lote se habian gastado
    en la llamada a la API de embeddings.
    """
    espera = 1.0
    ultimo_error = None
    for intento in range(reintentos_max + 1):
        try:
            supabase.table("normativa_bancaria").insert(filas).execute()
            return len(filas)
        except Exception as exc:
            ultimo_error = exc
            if intento == reintentos_max:
                break
            time.sleep(espera)
            espera = min(espera * 2, 30.0)
    raise RuntimeError(
        f"insercion fallo tras {reintentos_max + 1} intentos: {ultimo_error}"
    )


def nombre_documento(url: str) -> str:
    partes = urlparse(url)
    return (partes.netloc + partes.path).strip("/").replace("/", "_") or "doc"


def ingesta_por_coleccion(
    urls: list,
    coleccion_id: str,
    chunk_size: int,
    chunk_overlap: int,
    workers: int,
    ai_client,
    supabase,
    timeout: float = 20.0,
    reintentos_url: int = 2,
    sin_bd: bool = False,
    solo_red_cpu: bool = False,
) -> dict:
    metricas = Metricas()
    t_inicio = time.perf_counter()
    errores = []

    # Nota sobre sincronizacion y ausencia de condiciones de carrera:
    # ningun worker escribe directamente en 'metricas' ni en 'errores'. Ambos son
    # mutados exclusivamente por este hilo, que es el unico que drena
    # as_completed(). Por eso no hace falta Lock: un Lock alrededor de esas
    # escrituras seria redundante y solo ocultaria la invariante de que el hilo
    # principal es el unico escritor. Cada worker de descarga usa ademas su propio
    # httpx.Client, de modo que no hay cliente HTTP compartido entre hilos.

    # Deduplicacion preservando el orden de entrada. Sin esto, dos URLs iguales
    # dentro del mismo lote de peticiones pasarian ambas el chequeo de duplicados
    # (la fila todavia no existe en BD) y se insertarian duplicadas.
    urls_unicas = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
    metricas.urls_duplicadas = len(urls) - len(urls_unicas)
    if metricas.urls_duplicadas:
        print(f"    [!] {metricas.urls_duplicadas} URL(s) duplicada(s) descartadas")

    with metricas.medir("scraping"):
        descargados = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futuros = {
                pool.submit(descargar_una, url, timeout, reintentos_url): url
                for url in urls_unicas
            }
            for fut in as_completed(futuros):
                res = fut.result()
                if res["ok"]:
                    metricas.url_completadas += 1
                    metricas.cpu_descarga += res["bytes"] / 1_000_000.0
                    descargados[res["url"]] = res
                else:
                    metricas.url_fallidas += 1
                    errores.append({
                        "fase": "scraping", "url": res["url"], "error": res["error"],
                    })
                    print(f"    [X] {res['url']} -> {res['error']}")

    pendientes = []
    # sin_bd y solo_red_cpu implican 'no tocar Supabase': sin check de duplicados
    # y sin insercion. En ambos casos se procesa todo lo descargado.
    sin_base = sin_bd or solo_red_cpu
    for indice, url in enumerate(urls_unicas):
        res = descargados.get(url)
        if res is None:
            continue
        doc = nombre_documento(url)
        if sin_base:
            pendientes.append((indice, doc, res))
            continue
        existe = (supabase.table("normativa_bancaria")
                  .select("id")
                  .eq("coleccion_id", coleccion_id)
                  .eq("documento_origen", doc)
                  .limit(1).execute())
        if not existe.data:
            pendientes.append((indice, doc, res))

    with metricas.medir("chunking"):
        bloques_por_indice = {}
        if workers > 1 and pendientes:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futuros = {
                    pool.submit(limpiar_y_trocear, res["html"], chunk_size, chunk_overlap): indice
                    for indice, doc, res in pendientes
                }
                for fut in as_completed(futuros):
                    indice = futuros[fut]
                    try:
                        bloques_por_indice[indice] = fut.result()
                    except Exception as exc:
                        # Un HTML malformado no debe abortar la ingesta completa.
                        bloques_por_indice[indice] = []
                        errores.append({
                            "fase": "chunking", "error": f"{type(exc).__name__}: {exc}",
                        })
                        print(f"    [X] documento #{indice} fallo al trocear: {exc}")
                    metricas.cpu_chunking += 1
        else:
            for indice, doc, res in pendientes:
                try:
                    bloques_por_indice[indice] = limpiar_y_trocear(
                        res["html"], chunk_size, chunk_overlap)
                except Exception as exc:
                    bloques_por_indice[indice] = []
                    errores.append({
                        "fase": "chunking", "error": f"{type(exc).__name__}: {exc}",
                    })
                metricas.cpu_chunking += 1

    # Ensamblado en el orden ORIGINAL de entrada. El orden de finalizacion de
    # as_completed() es no determinista (depende de quien termina primero), asi
    # que usar ese orden haria que 'articulo_ref' se renumerara en cada corrida
    # del mismo documento: las citas no serian estables y el benchmark no seria
    # reproducible. Aqui cada fragmento lleva su indice dentro de SU documento.
    tareas_chunk = []
    for indice, doc, _ in pendientes:
        for numero, bloque in enumerate(bloques_por_indice.get(indice, []), start=1):
            tareas_chunk.append((doc, numero, bloque))

    metricas.fragmentos = len(tareas_chunk)
    if not tareas_chunk:
        return {
            "coleccion_id": coleccion_id,
            "wallclock_s": round(time.perf_counter() - t_inicio, 2),
            "desglose_pct": metricas.desglose_pct(),
            "fragmentos": 0,
            "fragmentos_sin_embedding": 0,
            "insertados": 0,
            "url_ok": metricas.url_completadas,
            "url_fallidas": metricas.url_fallidas,
            "urls_duplicadas": metricas.urls_duplicadas,
            "errores": errores,
            "metricas": dict(metricas.fases),
        }

    # Medicion controlada del paralelismo: solo red + CPU, sin llamar a la API.
    # Aísla el speedup real de la concurrencia del techo impuesto por el
    # rate limit de la API de embeddings (Ley de Amdahl, punto 4.4).
    if solo_red_cpu:
        metricas.registrar_fase("embeddings", 0.0)
        metricas.registrar_fase("insercion", 0.0)
        return {
            "coleccion_id": coleccion_id,
            "wallclock_s": round(time.perf_counter() - t_inicio, 2),
            "desglose_pct": metricas.desglose_pct(),
            "metricas": metricas.__dict__,
            "fragmentos": len(tareas_chunk),
            "insertados": 0,
            "url_ok": metricas.url_completadas,
            "url_fallidas": metricas.url_fallidas,
            "errores": errores,
        }

    with metricas.medir("embeddings"):
        vectores = [None] * len(tareas_chunk)
        for numero_lote, inicio in enumerate(
            range(0, len(tareas_chunk), BATCH_SIZE), start=1
        ):
            lote = tareas_chunk[inicio:inicio + BATCH_SIZE]
            try:
                lote_texto = [c[2] for c in lote]
                obtenidos = generar_embeddings(ai_client, lote_texto)
                if len(obtenidos) != len(lote):
                    raise RuntimeError(
                        f"la API devolvio {len(obtenidos)} vectores para {len(lote)} fragmentos"
                    )
                vectores[inicio:inicio + len(lote)] = obtenidos
            except Exception as exc:
                # Los vectores de este lote quedan en None y el fragmento se
                # reporta como perdido; no se degrada a vector de ceros, que
                # contaminaria la busqueda vectorial con falsos positivos.
                errores.append({
                    "fase": "embeddings", "lote": numero_lote,
                    "fragmentos": len(lote), "error": str(exc)[:200],
                })
                print(f"    [X] Lote {numero_lote} fallo: {str(exc)[:140]}")
            time.sleep(PAUSA_ENTRE_LOTES)

    metricas.fragmentos_sin_embedding = sum(1 for v in vectores if v is None)

    with metricas.medir("insercion"):
        filas = [
            {
                "documento_origen": doc,
                "coleccion_id": coleccion_id,
                "organismo": urlparse(doc).netloc,
                "tipo_norma": "Web / Portal normativo",
                "jerarquia": coleccion_id,
                "articulo_ref": f"Fragmento {numero}",
                "contenido": contenido,
                "embedding": vector,
            }
            for (doc, numero, contenido), vector in zip(tareas_chunk, vectores)
            if vector is not None
        ]
        if sin_bd:
            metricas.inserted = 0
        else:
            for numero_lote, inicio in enumerate(
                range(0, len(filas), BATCH_SIZE), start=1
            ):
                lote = filas[inicio:inicio + BATCH_SIZE]
                try:
                    metricas.inserted += insertar_lote(supabase, lote)
                except Exception as exc:
                    # Un lote fallido no aborta los siguientes.
                    errores.append({
                        "fase": "insercion", "lote": numero_lote,
                        "fragmentos": len(lote), "error": str(exc)[:200],
                    })
                    print(f"    [X] Lote {numero_lote} no insertado: {str(exc)[:140]}")

    wallclock = time.perf_counter() - t_inicio
    return {
        "coleccion_id": coleccion_id,
        "wallclock_s": round(wallclock, 2),
        "desglose_pct": metricas.desglose_pct(),
        "fragmentos": metricas.fragmentos,
        "fragmentos_sin_embedding": metricas.fragmentos_sin_embedding,
        "insertados": metricas.inserted,
        "url_ok": metricas.url_completadas,
        "url_fallidas": metricas.url_fallidas,
        "urls_duplicadas": metricas.urls_duplicadas,
        "errores": errores,
        "metricas": dict(metricas.fases),
    }


def ejecutar_benchmark(urls: list, workers: list, args) -> dict:
    solo_red_cpu = getattr(args, "solo_red_cpu", False)
    sin_bd = getattr(args, "sin_bd", False)
    ai_client = (None if solo_red_cpu
                 else genai.Client(api_key=os.environ["GEMINI_API_KEY"]))
    supabase = (None if (sin_bd or solo_red_cpu)
                else create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]))
    filas = []
    t1 = None
    for p in workers:
        coleccion = f"bench_p{p}_{int(time.time())}"
        res = ingesta_por_coleccion(
            urls=urls,
            coleccion_id=coleccion,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            workers=p,
            ai_client=ai_client,
            supabase=supabase,
            timeout=args.timeout,
            sin_bd=getattr(args, "sin_bd", False),
            solo_red_cpu=getattr(args, "solo_red_cpu", False),
        )
        if supabase is not None:
            supabase.table("normativa_bancaria").delete().eq("coleccion_id", coleccion).execute()
        if p == 1:
            t1 = res["wallclock_s"]
        filas.append({
            "workers": p,
            "T_s": res["wallclock_s"],
            "speedup": round(t1 / res["wallclock_s"], 3) if res["wallclock_s"] else None,
            "eficiencia": round(t1 / res["wallclock_s"] / p, 3) if res["wallclock_s"] else None,
            "desglose_pct": res["desglose_pct"],
        })
        print(f"    p={p:>2}  T={res['wallclock_s']:>7.2f}s  S={filas[-1]['speedup']}  E={filas[-1]['eficiencia']}  "
              f"desglose={res['desglose_pct']}")
    return {"filas": filas, "t1": t1}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingesta web concurrente multi-tenant.")
    sub = parser.add_subparsers(dest="comando", required=True)

    p_ing = sub.add_parser("ingerir", help="Ingesta una coleccion desde URLs.")
    p_ing.add_argument("--urls", required=True, help="Ruta a archivo con una URL por linea (o JSON con lista).")
    p_ing.add_argument("--coleccion", required=True)
    p_ing.add_argument("--chunk-size", type=int, default=1000)
    p_ing.add_argument("--chunk-overlap", type=int, default=200)
    p_ing.add_argument("--workers", type=int, default=4)
    p_ing.add_argument("--timeout", type=float, default=20.0)
    p_ing.add_argument("--sin-bd", action="store_true",
                       help="Omite Supabase (check de duplicados e insercion); mide solo red, CPU y embeddings.")
    p_ing.add_argument("--solo-red-cpu", action="store_true",
                       help="Omite Supabase y la API de embeddings; mide solo scraping y chunking.")

    p_bench = sub.add_parser("benchmark", help="Compara T_s vs T_p y calcula S_p, E_p.")
    p_bench.add_argument("--urls", required=True)
    p_bench.add_argument("--chunk-size", type=int, default=1000)
    p_bench.add_argument("--chunk-overlap", type=int, default=200)
    p_bench.add_argument("--timeout", type=float, default=20.0)
    p_bench.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    p_bench.add_argument("--sin-bd", action="store_true",
                         help="Benchmark sin Supabase (aísla el efecto de red/CPU en la aceleración).")
    p_bench.add_argument("--solo-red-cpu", action="store_true",
                         help="Mide solo scraping + chunking, sin llamar a la API de embeddings. "
                              "Permite medir el speedup real de la concurrencia sin que el "
                              "rate limit de la API (429) contamine los tiempos.")

    args = parser.parse_args()
    # Un --workers <= 0 hace que ThreadPoolExecutor levante ValueError con un
    # mensaje poco util; se valida aqui para dar un error accionable.
    if getattr(args, "workers", None) is not None:
        valores = args.workers if isinstance(args.workers, list) else [args.workers]
        if any(w < 1 or w > 32 for w in valores):
            print("[X] --workers debe estar entre 1 y 32.")
            return
    if args.comando == "ingerir" and args.chunk_overlap >= args.chunk_size:
        print("[X] --chunk-overlap debe ser menor que --chunk-size.")
        return
    urls = leer_urls(args.urls)
    if not urls:
        print("[X] No se encontraron URLs.")
        return

    if args.comando == "ingerir":
        ai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        supabase = (None if args.sin_bd
                    else create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]))
        res = ingesta_por_coleccion(
            urls=urls, coleccion_id=args.coleccion, chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap, workers=args.workers,
            ai_client=ai_client, supabase=supabase, timeout=args.timeout,
            sin_bd=args.sin_bd, solo_red_cpu=args.solo_red_cpu,
        )
        print(json.dumps(res, indent=2, default=str))
    else:
        modo = []
        if args.sin_bd:
            modo.append("SIN BD")
        if args.solo_red_cpu:
            modo.append("SOLO RED+CPU")
        sufijo = f" | MODO {' / '.join(modo)}" if modo else ""
        print(f"[*] Benchmark concurrente sobre {len(urls)} URLs | workers={args.workers}{sufijo}")
        resultado = ejecutar_benchmark(urls, args.workers, args)
        print("\n=== TABLA COMPARATIVA ===")
        print(f"{'p':>3} | {'T_p (s)':>9} | {'S_p':>6} | {'E_p':>6}")
        for f in resultado["filas"]:
            print(f"{f['workers']:>3} | {f['T_s']:>9.2f} | {f['speedup']:>6} | {f['eficiencia']:>6}")
        with open("resultados_benchmark.json", "w") as fh:
            json.dump(resultado, fh, indent=2)
        print("[✓] Resultados en resultados_benchmark.json")


def leer_urls(ruta: str) -> list:
    with open(ruta) as fh:
        crudo = fh.read().strip()
    if crudo.startswith("["):
        return json.loads(crudo)
    return [ln.strip() for ln in crudo.splitlines() if ln.strip() and not ln.startswith("#")]


if __name__ == "__main__":
    main()

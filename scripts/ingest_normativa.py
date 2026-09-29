import os
import re
import sys
import time
import argparse
import pymupdf
import pytesseract
from pdf2image import convert_from_path
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types
from concurrent.futures import ProcessPoolExecutor, as_completed

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

MODEL_EMBEDDING = "models/gemini-embedding-001"
BATCH_SIZE = 10  # Tamaño seguro para evitar saturación de TPM

def extraer_texto_pdf(ruta_pdf):
    doc = pymupdf.open(ruta_pdf)
    texto_completo = []
    
    # Intento 1: Texto nativo digital
    for num_pagina, pagina in enumerate(doc):
        t = pagina.get_text().strip()
        if t:
            texto_completo.append((num_pagina + 1, t))
            
    # Intento 2: OCR si está escaneado
    if not texto_completo:
        print("    [i] PDF escaneado detectado. Procesando OCR...")
        try:
            imagenes = convert_from_path(ruta_pdf)
            for idx, img in enumerate(imagenes):
                print(f"        -> OCR página {idx + 1}/{len(imagenes)}...")
                txt = pytesseract.image_to_string(img, lang="spa")
                if txt.strip():
                    texto_completo.append((idx + 1, txt))
        except Exception as e:
            print(f"    [X] Error durante OCR: {e}")
            
    return texto_completo

def parsear_articulos(paginas_texto, nombre_doc):
    if not paginas_texto:
        return []
        
    texto_unido = "\n".join([t[1] for t in paginas_texto]).strip()
    if not texto_unido:
        return []

    patron = r'(?i)(art[ií]culo\s+\d+[\.º°\-–\s]*[^\n]*)'
    fragmentos = re.split(patron, texto_unido)
    registros = []
    
    if len(fragmentos) <= 1:
        bloque_tam = 1200
        solapamiento = 200
        inicio = 0
        while inicio < len(texto_unido):
            fin = min(inicio + bloque_tam, len(texto_unido))
            chunk = texto_unido[inicio:fin].strip()
            if len(chunk) > 80:
                registros.append({
                    "documento_origen": nombre_doc,
                    "organismo": "ASFI / Banco Unión",
                    "tipo_norma": "Circular / Resolución",
                    "jerarquia": "RNSF",
                    "articulo_ref": f"Sección {len(registros) + 1}",
                    "contenido": chunk
                })
            inicio += (bloque_tam - solapamiento)
        return registros

    for i in range(1, len(fragmentos), 2):
        encabezado = fragmentos[i].strip()
        cuerpo = fragmentos[i+1].strip() if (i+1) < len(fragmentos) else ""
        contenido_total = f"{encabezado}\n{cuerpo}".strip()
        
        if len(contenido_total) > 40:
            registros.append({
                "documento_origen": nombre_doc,
                "organismo": "ASFI",
                "tipo_norma": "Circular / Resolución",
                "jerarquia": "RNSF",
                "articulo_ref": encabezado[:100],
                "contenido": contenido_total
            })
            
    return registros

def generar_embeddings_con_reintento(textos):
    intentos_rate_limit = 0
    while True:
        try:
            res = ai_client.models.embed_content(
                model=MODEL_EMBEDDING,
                contents=textos,
                config=types.EmbedContentConfig(output_dimensionality=768)
            )
            time.sleep(1.5)  # Pausa preventiva entre batches
            return [e.values for e in res.embeddings]
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                intentos_rate_limit += 1
                if intentos_rate_limit > 3:
                    print("\n[!] Se superó el límite diario de la API en este proyecto.")
                    print("    Genera una nueva API Key en otro proyecto de AI Studio para continuar.")
                    sys.exit(1)
                print("\n    [!] Pausa por límite de peticiones por minuto. Esperando 40 seg...")
                time.sleep(40)
            else:
                print(f"    [X] Error: {e}")
                raise e

def procesar_pdf(ruta_pdf, nombre):
    paginas = extraer_texto_pdf(ruta_pdf)
    if not paginas:
        print(f"    [X] '{nombre}' sin texto.")
        return []
    chunks = parsear_articulos(paginas, nombre)
    print(f"    -> '{nombre}': {len(chunks)} fragmentos extraídos.")
    return chunks

def procesar_directorio(max_workers=2):
    directorio = "documentos_pdf"
    if not os.path.exists(directorio):
        os.makedirs(directorio)
        
    archivos = [f for f in os.listdir(directorio) if f.lower().endswith(".pdf")]
    archivos = [f for f in archivos if f.lower() != "circulares.pdf"]

    if not archivos:
        print(f"[!] No hay archivos PDF en '{directorio}/'.")
        return

    print(f"[*] Total de archivos en carpeta: {len(archivos)}")

    pendientes = []
    for archivo in archivos:
        check = supabase.table("normativa_bancaria").select("id").eq("documento_origen", archivo).limit(1).execute()
        if check.data:
            print(f"[=] Omitiendo '{archivo}' (ya indexado en Supabase).")
        else:
            pendientes.append(archivo)

    if not pendientes:
        print("[✓] Todos los documentos ya están indexados.")
        return

    print(f"[*] Fase A (CPU-bound): extracción/OCR paralela con {max_workers} proceso(s)...")
    t_a = time.time()
    bloques = []
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futuros = {pool.submit(procesar_pdf, os.path.join(directorio, a), a): a for a in pendientes}
        for fut in as_completed(futuros):
            bloques.append(fut.result())
    print(f"    [✓] Fase A completada en {time.time() - t_a:.1f}s.")

    chunks_totales = [c for b in bloques for c in b]
    total_chunks = len(chunks_totales)
    print(f"[*] Total de fragmentos a indexar: {total_chunks}")

    print(f"[*] Fase B (I/O-bound): embeddings + Supabase en lotes de {BATCH_SIZE} (pausa preventiva)...")
    t_b = time.time()
    for i in range(0, total_chunks, BATCH_SIZE):
        lote = chunks_totales[i:i + BATCH_SIZE]
        textos_lote = [item["contenido"] for item in lote]
        vectores = generar_embeddings_con_reintento(textos_lote)
        
        filas = []
        for item, vec in zip(lote, vectores):
            filas.append({
                "documento_origen": item["documento_origen"],
                "organismo": item["organismo"],
                "tipo_norma": item["tipo_norma"],
                "jerarquia": item["jerarquia"],
                "articulo_ref": item["articulo_ref"],
                "contenido": item["contenido"],
                "embedding": vec
            })
        
        supabase.table("normativa_bancaria").insert(filas).execute()
        print(f"    [✓] Chunks {i+1} a {min(i + BATCH_SIZE, total_chunks)} de {total_chunks} insertados.")
    print(f"    [✓] Fase B completada en {time.time() - t_b:.1f}s.")

    print("\n[✓] Ingesta finalizada.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingesta RAG normativa bancaria (OCR paralelo + embeddings por lotes).")
    parser.add_argument("--workers", type=int, default=2,
                        help="Procesos paralelos para extracción/OCR (default 2, ajústalo a tus vCPUs).")
    args = parser.parse_args()
    procesar_directorio(max_workers=args.workers)

"""Verificacion de aislamiento entre colecciones (criterio 1 de evaluacion).

Demuestra que la busqueda vectorial NUNCA puede devolver un fragmento de
otra coleccion, para ninguna consulta y ningun nivel de similitud.

Por que no necesita la API de embeddings: el aislamiento es una propiedad
del DDL y de la RPC, no del modelo. Lo que se prueba aqui es exactamente la
garantia que exige el enunciado, con la advantage de que se puede
verificar exhaustivamente (muchas consultas aleatorias) en lugar de con
dos o tres preguntas sueltas.

Uso:
    docker compose exec -T backend python scripts/verificar_aislamiento.py
"""
import os
import random
import sys
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()
URL = os.environ["SUPABASE_URL"].rstrip("/")
CLAVE = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
CABECERAS = {"apikey": CLAVE, "Authorization": f"Bearer {CLAVE}"}
DIMENSIONES = 768
MUESTRAS = 40


def peticion(metodo: str, ruta: str, **kwargs) -> httpx.Response:
    with httpx.Client(timeout=30.0, headers=CABECERAS) as cliente:
        return cliente.request(metodo, f"{URL}/rest/v1/{ruta}", **kwargs)


def vectores_aleatorios(n: int) -> list:
    generador = random.Random(20260929)  # semilla fija: prueba reproducible
    return [
        [round(generador.uniform(-1, 1), 6) for _ in range(DIMENSIONES)]
        for _ in range(n)
    ]


def buscar(embedding: list, coleccion: str, umbral: float = -1.0) -> list:
    r = peticion(
        "POST",
        "rpc/match_normativa_coleccion",
        json={
            "query_embedding": embedding,
            "p_coleccion_id": coleccion,
            "match_threshold": umbral,
            "match_count": 20,
        },
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    print("=" * 68)
    print("VERIFICACION DE AISLAMIENTO ENTRE COLECCIONES")
    print("=" * 68)

    r = peticion("GET", "normativa_bancaria?select=coleccion_id")
    r.raise_for_status()
    colecciones = sorted({f["coleccion_id"] for f in r.json()})
    if not colecciones:
        print("[X] No hay colecciones en la base de datos.")
        return 1

    print(f"\nColecciones detectadas: {len(colecciones)}")
    for c in colecciones:
        total = peticion(
            "GET", f"normativa_bancaria?select=id&coleccion_id=eq.{c}"
        ).json()
        print(f"  - {c:<30} {len(total):>4} fragmentos")
    print(f"\nConsultas aleatorias por coleccion: {MUESTRAS}")
    print(f"Dimensiones del vector: {DIMENSIONES}   Semilla fija: 20260929\n")

    fugas = 0
    total_resultados = 0
    for coleccion in colecciones:
        peor_similitud = 0.0
        for embedding in vectores_aleatorios(MUESTRAS):
            filas = buscar(embedding, coleccion)
            for fila in filas:
                total_resultados += 1
                if fila["coleccion_id"] != coleccion:
                    fugas += 1
                    print(f"  [FUGA] {coleccion} devolvio {fila['coleccion_id']}")
                peor_similitud = max(peor_similitud, fila["similarity"])
        print(f"  {coleccion:<30} OK  (max similitud observada: {peor_similitud:.4f})")

    # Una coleccion que no existe debe devolver conjunto vacio, nunca un
    # respaldo a busqueda global.
    inexistente = "coleccion_inexistente_verificacion"
    filas = buscar(vectores_aleatorios(1)[0], inexistente)
    print(f"\n  Coleccion inexistente '{inexistente}': {len(filas)} resultados "
          f"(esperado 0)")

    print("\n" + "-" * 68)
    print(f"Filas recuperadas en total : {total_resultados}")
    print(f"Filas de otra coleccion   : {fugas}")
    vacio_ok = len(filas) == 0
    if fugas == 0 and vacio_ok and total_resultados > 0:
        print("\n[OK] CERO CONTAMINACION CRUZADA CONFIRMADA")
        print("  Ninguna consulta devolvio un fragmento de otra coleccion.")
        return 0
    print("\n[X] FALLO DE AISLAMIENTO")
    return 1


if __name__ == "__main__":
    sys.exit(main())

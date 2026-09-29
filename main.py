import os
import sys
import time
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    raise RuntimeError("Faltan variables de entorno requeridas en el archivo .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

MODEL_EMBEDDING = "models/gemini-embedding-001"
MODELOS_GENERACION = ["gemini-3.6-flash", "gemini-3.5-flash"]

CONFIG_GENERACION = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(thinking_budget=0),
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    temperature=0.0,
)

app = FastAPI(
    title="API RAG Normativa Bancaria - ASFI / Banco Unión",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConsultaRequest(BaseModel):
    pregunta: str
    coleccion_id: str = Field(..., min_length=1, max_length=50,
                              description="Dominio temático obligatorio (aislamiento multi-tenant).")
    top_k: Optional[int] = 4
    match_threshold: Optional[float] = 0.35

class FuenteNormativa(BaseModel):
    documento: str
    articulo_ref: str
    similitud: float
    contenido: str

class ConsultaResponse(BaseModel):
    pregunta: str
    respuesta: str
    fuentes: List[FuenteNormativa]

def buscar_contexto(pregunta: str, coleccion_id: str, top_k: int, match_threshold: float):
    res_emb = ai_client.models.embed_content(
        model=MODEL_EMBEDDING,
        contents=[pregunta],
        config=types.EmbedContentConfig(output_dimensionality=768)
    )
    query_vector = res_emb.embeddings[0].values

    rpc_res = supabase.rpc("match_normativa_coleccion", {
        "query_embedding": query_vector,
        "p_coleccion_id": coleccion_id,
        "match_threshold": match_threshold,
        "match_count": top_k
    }).execute()

    return rpc_res.data or []

def generar_con_respaldo(prompt: str) -> str:
    """Genera respuesta con Gemini aplicando reintentos y fallback a modelo alternativo si hay 503."""
    ultimo_error = None
    for modelo in MODELOS_GENERACION:
        for intento in range(2):
            try:
                res = ai_client.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config=CONFIG_GENERACION
                )
                return res.text
            except Exception as e:
                ultimo_error = e
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    time.sleep(2)
                    continue
                else:
                    break
    raise HTTPException(status_code=503, detail=f"Servicio LLM no disponible temporalmente: {ultimo_error}")

@app.get("/")
def estado():
    return {"status": "online", "mensaje": "API RAG de Normativa Bancaria activa"}

@app.get("/api/colecciones")
def listar_colecciones():
    filas = (supabase.table("normativa_bancaria")
             .select("coleccion_id")
             .limit(1000).execute())
    conteo = {}
    for f in filas.data or []:
        conteo[f["coleccion_id"]] = conteo.get(f["coleccion_id"], 0) + 1
    return {"colecciones": [{"coleccion_id": k, "fragmentos": v} for k, v in sorted(conteo.items())]}

class PayloadIngesta(BaseModel):
    coleccion_id: str = Field(..., min_length=1, max_length=50)
    urls: List[str] = Field(..., min_items=1)
    chunk_size: int = 1000
    chunk_overlap: int = 200
    concurrency_workers: int = Field(4, ge=1, le=16)

@app.post("/api/ingestar-web")
def ingestar_web(payload: PayloadIngesta):
    if payload.chunk_overlap >= payload.chunk_size:
        raise HTTPException(status_code=400, detail="chunk_overlap debe ser menor que chunk_size.")
    from ingestar_web import ingesta_por_coleccion
    try:
        return ingesta_por_coleccion(
            urls=payload.urls,
            coleccion_id=payload.coleccion_id,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            workers=payload.concurrency_workers,
            ai_client=ai_client,
            supabase=supabase,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fallo en ingesta web: {e}")

@app.post("/api/consultar", response_model=ConsultaResponse)
def consultar_normativa(req: ConsultaRequest):
    if not req.pregunta.strip():
        raise HTTPException(status_code=400, detail="La pregunta no puede estar vacía.")

    try:
        docs = buscar_contexto(req.pregunta, req.coleccion_id, req.top_k, req.match_threshold)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en búsqueda vectorial: {e}")

    if not docs:
        return ConsultaResponse(
            pregunta=req.pregunta,
            respuesta=(f"La colección '{req.coleccion_id}' no contiene antecedentes que respongan "
                       "a esta consulta. No puedo emitir una respuesta fundamentada sin inventar información."),
            fuentes=[]
        )

    contexto_texto = ""
    fuentes_resp = []
    for d in docs:
        contexto_texto += f"\n--- DOCUMENTO: {d.get('documento_origen')} | REFERENCIA: {d.get('articulo_ref')} ---\n{d.get('contenido')}\n"
        fuentes_resp.append(FuenteNormativa(
            documento=d.get("documento_origen", ""),
            articulo_ref=d.get("articulo_ref", ""),
            similitud=round(d.get("similarity", 0) * 100, 2),
            contenido=d.get("contenido", "")
        ))

    prompt = f"""Eres un asesor legal especializado en normativa del dominio '{req.coleccion_id}'.
Responde de forma rigurosa, clara y estructurada utilizando EXCLUSIVAMENTE el siguiente contexto normativo.
Cita siempre el artículo o resolución de respaldo. Si algo no figura en el texto provisto, decláralo
expresamente: no completes, no deduzcas y no inventes referencias.
Si el contexto es insuficiente para responder, indícalo de manera explícita.

CONTEXTO NORMATIVO (colección: {req.coleccion_id}):
{contexto_texto}

PREGUNTA:
{req.pregunta}

RESPUESTA:"""

    respuesta_texto = generar_con_respaldo(prompt)

    return ConsultaResponse(
        pregunta=req.pregunta,
        respuesta=respuesta_texto,
        fuentes=fuentes_resp
    )

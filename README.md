# Asistente Normativo RAG — ASFI / Banco Unión

Sistema RAG multi-tenant para consultar normativa de la **ASFI** y de **Banco
Unión**, con ingesta concurrente de fuentes web y aislamiento estricto por
colección.

## Arquitectura

| Capa | Tecnología |
|---|---|
| Backend | FastAPI (`/api/consultar`, `/api/ingestar-web`, `/api/colecciones`) |
| Frontend | Streamlit (chat con selector de colección) |
| Vector store | Supabase + pgvector (768 dims), particionado por `coleccion_id` |
| Ingesta PDF | PyMuPDF + Tesseract OCR (`ProcessPoolExecutor`) |
| Ingesta web | httpx + BeautifulSoup/lxml (`ThreadPoolExecutor`) |
| Embeddings | `gemini-embedding-001` (768d) |
| LLM | `gemini-3.6-flash`, fallback `gemini-3.5-flash`, `temperature=0.0` |

## Levantar el entorno

Requiere Docker. Un solo comando levanta backend y frontend:

```bash
cp .env.example .env    # completa SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY
docker compose up --build
```

- API / Swagger: <http://localhost:8000/docs>
- Chat: <http://localhost:8501>

La base de datos es externa (Supabase) y se conecta por variables de entorno.
Para crearla, ejecutar `scripts/schema.sql` y luego `scripts/schema_colecciones.sql`
en el SQL Editor del proyecto.

El scraper se invoca bajo demanda, sin depender de la API:

```bash
docker compose --profile tools run --rm scraper \
  benchmark --urls scripts/urls_prueba.txt --workers 1 2 4 8 --solo-red-cpu
```

## Ingesta de una colección

```bash
curl -X POST http://localhost:8000/api/ingestar-web \
  -H "Content-Type: application/json" \
  -d '{"coleccion_id":"mi_coleccion","urls":["https://ejemplo.org/doc"],"concurrency_workers":4}'
```

Cada consulta exige `coleccion_id` y la RPC `match_normativa_coleccion` acota la
búsqueda a esa colección, de modo que dos dominios distintos nunca mezclan
vectores.

## Documentación

| Documento | Contenido |
|---|---|
| [`GUIA_DE_USO.md`](GUIA_DE_USO.md) | Guía operativa: despliegue, endpoints, pipeline y benchmark |
| [`REPORTE_BENCHMARK.md`](REPORTE_BENCHMARK.md) | Análisis experimental y Ley de Amdahl |
| [`scripts/schema.sql`](scripts/schema.sql) | Esquema base (pgvector, índices, RPC) |
| [`scripts/schema_colecciones.sql`](scripts/schema_colecciones.sql) | Migración multi-tenant |

## Estructura

```
├── main.py                     API: consultas e ingesta web
├── app_streamlit.py            Chat con selector de colección
├── scripts/
│   ├── schema.sql              Esquema base de Supabase
│   ├── schema_colecciones.sql  Migración multi-tenant
│   ├── ingestar_web.py         Scraper concurrente y benchmark
│   ├── ingest_normativa.py     Ingesta OCR de PDFs
│   └── graficos_benchmark.py   Gráficos y tablas desde los JSON
├── resultados_benchmark_*.json Resultados medidos
└── grafico_*.png               Figuras del reporte
```

## Desarrollo fuera de Docker

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload --port 8000
streamlit run app_streamlit.py
```

> Tras modificar `.env` en Docker usa `docker compose up -d --force-recreate`:
> `restart` no recarga el entorno del contenedor.

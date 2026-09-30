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
| [`REPORTE_BENCHMARK.md`](REPORTE_BENCHMARK.md) | Análisis experimental (3 réplicas) y verificación de Amdahl |
| [`scripts/schema.sql`](scripts/schema.sql) | Esquema base (pgvector, índices, RPC) |
| [`scripts/schema_colecciones.sql`](scripts/schema_colecciones.sql) | Migración multi-tenant |
| [`scripts/verificar_aislamiento.py`](scripts/verificar_aislamiento.py) | Test de cero contaminación cruzada |

## Criterios de evaluación

| Criterio | Evidencia |
|---|---|
| **Aislamiento de dominios en BD** | `schema_colecciones.sql`: columna `coleccion_id`, índice B-Tree, índice compuesto y RPC PL/pgSQL `match_normativa_coleccion` con filtro `n.coleccion_id = p_coleccion_id` dentro de la función y guarda de colección inexistente. Verificado con `scripts/verificar_aislamiento.py`: 120 consultas, 2400 filas, **0 de otra colección**. Plan de ejecución verificado con `EXPLAIN ANALYZE`: `Index Scan using idx_normativa_coleccion_btree` + `top-N heapsort`, 5.5 ms. |
| **Implementación concurrente** | `ThreadPoolExecutor` para descarga (I/O), `ProcessPoolExecutor` para limpieza y particionado (CPU), ambos en `ingestar_web.py`. Cada hilo usa su propio `httpx.Client`; `metricas` y `errores` los muta solo el hilo principal que drena `as_completed()`, por eso no hace falta `Lock`. Excepciones por fase registradas y devueltas; URLs caídas o lentas no abortan el lote. |
| **Lotes y rate limiting** | Embeddings por lotes de 10 con reintentos exponenciales 2 s → 60 s ante `429`/`RESOURCE_EXHAUSTED`/`503`; pausa entre lotes; inserción masiva por lotes con reintentos propios. Un lote fallido no aborta los siguientes y se reporta en `errores`. |
| **Análisis experimental** | `REPORTE_BENCHMARK.md`: `T_s`, `T_p`, `S_p`, `E_p` para `p={1,2,4,8}` con **media de 3 réplicas** (desv y rango incluidos), tablas comparativas, desglose temporal porcentual, y Amdahl usado como chequeo de consistencia (predice 2.67 frente a 2.70 medido en `p=8`), no como techo. Series en `resultados_benchmark_*.json`, `resultados_replica_*.json` y figuras `grafico_*.png`. |
| **Contenedorización y reproducibilidad** | `Dockerfile` con Tesseract (es), poppler, libmupdf; `docker-compose.yml` con backend, frontend, healthcheck y red `rag_net`. Verificado desde un clon limpio con `docker compose up --build`. |

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

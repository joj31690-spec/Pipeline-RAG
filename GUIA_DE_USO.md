# GUÍA DE USO — Sistema RAG ASFI / Banco Unión (lexbancario-ai)

Documento de referencia para operar el sistema desplegado en Docker, entender la
arquitectura y comprender la Fase 4 (Sistemas Paralelos) de la práctica de laboratorio.

---

## 1. ¿Qué busca este proyecto?

Es un **asistente de consulta normativa** multi-tenant basado en RAG (Retrieval-
Augmented Generation): el usuario formula preguntas en lenguaje natural sobre
normativa de la **ASFI (Autoridad de Supervisión del Sistema Financiero de
Bolivia)** y circulares aplicables a **Banco Unión**, y el sistema responde
citando las resoluciones o artículos de respaldo.

Cada consulta se ejecuta **dentro de una colección**. La colección aísla el corpus:
una búsqueda jamás puede recuperar fragmentos de otra colección, ni siquiera si la
pregunta pertence temáticamente a otro dominio.

Flujo típico de una consulta:

```
Usuario (Streamlit UI)                     FastAPI (backend)
        |  pregunta: "¿facultades de       |
        |  fiscalización?" + coleccion_id  |
        |                         v
        |                         1. Gemini embedding-001  -> vector 768d de la pregunta
        |                         2. búsqueda vectorial en Supabase (pgvector)
        |                            RPC "match_normativa_coleccion"
        |                            (filtro determinista n.coleccion_id = p_coleccion_id)
        |                         3. contexto recuperado (top_k documentos)
        |                         4. Gemini-3.6-flash genera respuesta con citas
        v                            temperature = 0.0 (determinista)
   Respuesta + fuentes (artículo, similitud, fragmento)
```

Componentes:

| Capa | Tecnología | Rol |
|---|---|---|
| Frontend | Streamlit (puerto 8501) | Chat, selector de colección, slider top_k y umbral de similitud |
| Backend | FastAPI (puerto 8000) | API `/api/consultar`, `/api/ingestar-web`, `/api/colecciones` |
| Vector store | Supabase + pgvector (768 dims) | Tabla `normativa_bancaria` con embeddings y `coleccion_id` |
| Ingestion PDF | PyMuPDF + Tesseract OCR | Extracción de texto / OCR de PDFs escaneados |
| Ingestion web | httpx + BeautifulSoup/lxml | Descarga y limpieza de HTML en paralelo |
| Embeddings | Gemini `embedding-001` (768d) | Vectorización de fragmentos y consultas |
| LLM | Gemini `3.6-flash` (fallback `3.5-flash`) | Redacción de la respuesta fundamentada |

Para que una pregunta tenga fuentes, primero hay que **ingerir** la normativa
(dividir los PDFs en fragmentos, vectorizarlos y guardarlos en Supabase).

---

## 2. Despliegue Docker

### Archivos de orquestación creados

- `Dockerfile` — imagen base `python:3.12-slim` con Tesseract (español), poppler,
  libmupdf y gcc; instala `requirements.txt`; expone los puertos 8000 y 8501.
- `docker-compose.yml` — dos servicios construidos desde el mismo Dockerfile,
   comunicados por la red bridge interna `rag_net`:
  - `lexbancario_backend`:  `uvicorn main:app --host 0.0.0.0 --port 8000`
  - `lexbancario_frontend`: `streamlit run app_streamlit.py --server.port 8501 ...`
- `.env` — credenciales: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEY`.

> Nota: el frontend llama al backend por `http://backend:8000` (nombre del servicio
> en la red interna), NO por `127.0.0.1`, para que la comunicación ocurra entre
> contenedores.

### Comandos Docker, qué hacen y cuándo usarlos

| Comando | Qué hace |
|---|---|
| `docker compose up --build -d` | Construye imágenes y levanta ambos contenedores en segundo plano. **Primera vez / tras cambiar código.** |
| `docker compose ps` | Estado de los contenedores, puertos mapeados. |
| `docker compose logs -f backend` | Sigue los logs del backend (api request, errores). `-f` = seguir. |
| `docker compose exec backend bash` | Abre terminal interactiva dentro del contenedor backend (ya tiene Tesseract, poppler y Python). |
| `docker compose exec backend python scripts/ingest_normativa.py --workers 2` | Ejecuta la ingesta vectorial de PDFs **dentro** del contenedor (Fases A/B). |
| `docker stats lexbancario_backend` | Monitoreo en vivo de CPU/memoria del backend (Fase 4, métricas). |
| `docker compose up -d --force-recreate backend frontend` | Reinicia los servicios **recargando `.env`**. Es el comando correcto tras editar credenciales. |
| `docker compose down` | Detiene y elimina contenedores/red (los volúmenes bind `.:/app` conservan el código). |
| `docker compose exec -T backend python -m py_compile scripts/ingest_normativa.py` | Chequeo rápido de sintaxis del script. |

> ⚠️ `docker compose restart` **no** recarga las variables de entorno: reinicia el
> proceso con el entorno ya congelado en el contenedor. Tras editar `.env` usa
> siempre `up -d --force-recreate`.

### Verificación

- Backend API: `http://localhost:8000/docs` (Swagger) o `GET /` → `{"status":"online",...}`
- Frontend chat: `http://localhost:8501`

> `5173` y `5174` están ocupados por otras prácticas; esta usa `8000` y `8501`, no hay conflicto.

---

## 3. Fases de la práctica y estado actual

1. **Fase 1 — Clonado y entorno**: repositorio clonado en `~/paralelo/Normativa RAG`,
   `.env` generado desde `.env.example`. ✅
2. **Fase 2 — Dockerización**: Dockerfile + docker-compose buildsados y en marcha
   (`lexbancario_backend`, `lexbancario_frontend`). ✅
3. **Fase 3 — Ingesta en caliente**: 25 circulares ASFI descargadas del portal
   (`documentos_pdf/`, 28 MB). Ingesta vectorial ejecutada con credenciales reales:
   3 PDFs (22 fragmentos) quedaron cargados antes de pausar el proyecto Supabase;
   el proyecto fue restaurado y la migración `coleccion_id` aplicada, por lo que
   esas filas quedaron retroalimentadas a `asfi_bancaria_2026`. ✅
4. **Fase 4 — Sistemas Paralelos**: métricas, análisis y paralelización completados
   (ver sección 4). ✅
5. **Práctica de ingesta web concurrente y multi-tenant**: DDL `coleccion_id`,
   scraper concurrente, endpoint de ingesta, aislamiento verificado y benchmark
   `p={1,2,4,8}` (ver secciones 5 y 6). ✅

---

## 4. Fase 4 en detalle (Sistemas Paralelos)

### 4.1 Métricas de Rendimiento (`docker stats`)

Durante el OCR de `ASFI-150.pdf` (77 págs, 100 % escaneado):

| Muestra | CPU % | Memoria | Observación |
|---|---|---|---|
| Backend en reposo | 4.8 % | 173 MiB | Sólo uvicorn |
| Rasterización poppler | 96 % | 522 MiB | `convert_from_path` llena buffers de imagen |
| OCR Tesseract | 94 % | 841 MiB (10.6 %) | Satura **un** núcleo |

- Host/VM: **2 vCPUs** y el contenedor ve ambos.
- Costo de Tesseract: **~19–25 s por página escaneada** (idioma `spa`).
- Estimación: el PDF más pesado (77 págs) ≈ **25 minutos** sólo de OCR.

Interpretación: la ingesta original dejaba ocioso un núcleo del host; el OCR es
el cómputo dominante (CPU-bound), verificable porque `docker stats` marca ~95 % en
un único core.

### 4.2 Análisis de Concurrencia vs Rate Limiting

Cuellos de botella identificados en `scripts/ingest_normativa.py`:

1. **CPU-bound — extracción/OCR** (`extraer_texto_pdf`, l.25–48): PyMuPDF + en
   escaneados rasterización con poppler y Tesseract página a página, todo en bucle
   secuencial. Es el factor que domina el tiempo total.
2. **I/O-bound — Google AI Studio** (`generar_embeddings_con_reintento`, l.98–121):
   batch de 10 fragmentos, pausa preventiva `sleep(1.5)` entre lotes (l.107);
   ante `429 RESOURCE_EXHAUSTED` espera **40 s** (l.117–118) y abandona tras 3
   agotamientos (l.113). Limite de tokens por minuto (TPM) = limiter global.
3. **I/O-bound — Supabase** (insert por lote, l.172).
4. **Diseño original serial**: procesaba un PDF completo a la vez; mientras la CPU
   hacía OCR, la red quedaba ociosa y viceversa. **Sin aprovechar el 2º núcleo.**

Conclusión conceptual: es un pipeline **mixto** — OCR es cómputo (CPU), embedding e
insert son espera de red (I/O). La concurrencia debe segmentarse por tipo y el rate
limiting de la API externa debe respetarse (no rafagas paralelas de embeddings).

### 4.3 Paralelización implementada

Modificación de `scripts/ingest_normativa.py` (CLI: `--workers N`, default 2):

- **Fase A — CPU-bound**: `ProcessPoolExecutor` extrae + parsea **todos los PDFs
  pendientes en paralelo** (`procesar_pdf`), usando ambos núcleos. No toca la red.
- **Fase B — I/O-bound**: tras extraer todo, los fragmentos se vectorizan e insertan
  en lotes de `BATCH_SIZE=10` **secuencialmente**, conservando la pausa de 1.5 s y
  los reintentos de 40 s: no se exceden las cuotas de Gemini.
- La fase B ya no se intercala con el OCR: la red avanza de forma continua sin
  paradas de espera por CPU.

### 4.4 Resultados del benchmark (4 PDFs escaneados, 16 págs)

| Modo | Tiempo | Speedup |
|---|---|---|
| Secuencial (versión original) | 473.7 s (~7.9 min) | 1.00x |
| Paralelo (2 workers) | 349.5 s (~5.8 min) | **1.36x** |

Fragmentos extraídos: ASFI_022→3, ASFI_018→4, ASFI_007→3, ASFI_015→27.

**¿Por qué 1.36x y no 2x?** El pool reparte **PDFs completos** de forma greedy: el
documento más largo (ASFI_015, 7 págs ≈ 2.3 min) predomina en la cola final y no hay
balanceo fino. Además se paga el coste de fork/overhead de cada proceso: medido de
forma aislada con `os.times()`, ese overhead **domina por completo** cuando las
tareas son pocas y grandes (con 8 documentos, `p=8` resulta más lento que `p=1`;
ver §3.2 de `REPORTE_BENCHMARK.md`).
**Extensión natural:** partir el trabajo por *páginas* en lugar de por PDF para
acercarse al speedup lineal (~2x).

---

## 5. Práctica: ingesta web concurrente y aislamiento por colección

### 5.1 Modelo de datos multi-tenant

Todo fragmento lleva `coleccion_id VARCHAR(50) NOT NULL`. El DDL está en
`scripts/schema_colecciones.sql` (aplicar en el SQL Editor de Supabase):

- Columna `coleccion_id` con default `asfi_bancaria_2026` (retroalimenta las filas
  existentes sin reescribirlas).
- Índice **B-Tree** en `coleccion_id`, como pide el enunciado para el filtrado
  determinista por colección.
- Índice compuesto `(coleccion_id, documento_origen)` para el check de duplicados.
- Función PL/pgSQL `match_normativa_coleccion(query_embedding, p_coleccion_id,
  match_threshold, match_count)`.

Detalles de la función que sostienen el aislamiento:

```sql
-- Guarda de integridad: colección inexistente -> conjunto vacío explícito
if v_faltantes = 0 then
    return;
end if;
-- Filtro determinista, sin fallback posible a búsqueda global
where n.coleccion_id = p_coleccion_id
```

El filtro vive **dentro de la función**, no en el cliente: aunque un cliente
enviara una colección equivocada, la búsqueda quedaría acotada igual.

> Nota de PL/pgSQL: `coleccion_id` es ambiguo dentro de la función porque existe
> como columna y como variable de salida de `RETURNS TABLE`. Por eso todas las
> referencias se califican con el alias `n` (o con el nombre de la tabla).

### 5.2 Endpoint de ingesta web

`POST /api/ingestar-web`:

```bash
curl -X POST http://localhost:8000/api/ingestar-web \
  -H "Content-Type: application/json" \
  -d '{
        "coleccion_id": "tributaria_bolivia_2026",
        "urls": ["https://es.wikipedia.org/wiki/Tributo"],
        "chunk_size": 1000,
        "chunk_overlap": 200,
        "concurrency_workers": 4
      }'
```

Responde con el desglose temporal porcentual por fase:

```json
{
  "coleccion_id": "tributaria_bolivia_2026",
  "wallclock_s": 57.49,
  "desglose_pct": {"scraping": 1.49, "chunking": 0.56, "embeddings": 88.79, "insercion": 9.15},
  "fragmentos": 90, "insertados": 80,
  "url_ok": 4, "url_fallidas": 0
}
```

Otros endpoints: `GET /api/colecciones` lista las colecciones con su conteo, y
`POST /api/consultar` exige `coleccion_id` (campo obligatorio, sin default).

### 5.3 Pipeline concurrente (`scripts/ingestar_web.py`)

```
URLs ──ThreadPoolExecutor──▶ HTML crudo
                                   │
                            ProcessPoolExecutor
                                   │  (limpiar_html + trocear_texto en el worker)
                                   ▼
                            fragmentos
                                   │
                        lotes de 10 ──▶ embeddings (API, SECUENCIAL + backoff)
                                   │
                                   ▼
                        inserción Supabase
```

- Descarga con `ThreadPoolExecutor` (I/O-bound: cada URL abre su propio cliente
  `httpx`, sin cliente compartido ni problemas de thread-safety).
- Limpieza HTML y particionado con `ProcessPoolExecutor` (CPU-bound). Ambas tareas
  corren **dentro** del proceso-trabajador; si la limpieza se hiciera en el hilo
  principal, la fase `chunking` mediría trabajo secuencial y falsearía el speedup.
- Embeddings **secuenciales** en lotes de 10, con reintentos exponenciales
  (2 s → 60 s) ante `429 RESOURCE_EXHAUSTED`, `503` y `UNAVAILABLE`.
- Inserción masiva por lotes, con reintentos propios. Un lote fallido no aborta
  los siguientes.

### 5.4 Manejo de errores y sincronización

Ninguna excepción se traga: cada fase acumula sus fallos en `errores`, que la
respuesta devuelve al cliente.

| Situación | Comportamiento |
|---|---|
| URL caída, lenta o con error HTTP | Se reintenta con backoff; se registra y el resto de las URLs continúa |
| HTML malformado | Se registra; ese documento no aborta la ingesta |
| Lote de embeddings agota los reintentos | El lote queda sin vector, se cuenta en `fragmentos_sin_embedding` y se reporta con su causa |
| Lote de inserción falla | Se reintenta; si persiste, se reporta y los lotes siguientes continúan |
| `concurrency_workers` fuera de 1–16 | `422` de validación de la API; error explícito en la CLI |

Sincronización: `metricas` y `errores` los muta **únicamente** el hilo principal
que drena `as_completed()`. Los workers no los tocan, así que no hace falta
`Lock`; un candado alrededor de esas escrituras sería redundante. Cada worker de
descarga abre su propio `httpx.Client`, de modo que no hay cliente HTTP
compartido entre hilos.

El ensamblado de fragmentos se hace **en el orden original de las URLs**, no en el
orden de finalización de `as_completed()` (que depende de qué hilo termina
primero). Sin eso, `articulo_ref` se renumeraba en cada corrida del mismo
documento, con lo que las citas no eran estables. Verificado: el hash de la
secuencia de fragmentos es idéntico con 1, 2, 4 y 8 workers.

### 5.5 Aislamiento verificado (cero contaminación cruzada)

El aislamiento se verifica de dos formas complementarias.

**Verificación exhaustiva, sin depender de la API** (`scripts/verificar_aislamiento.py`):

El aislamiento es una propiedad del DDL y de la RPC, no del modelo de embeddings,
as que puede comprobarse exhaustivamente en vez de con dos o tres preguntas:

```bash
docker compose exec -T backend python scripts/verificar_aislamiento.py
```

```
Colecciones detectadas: 3
  asfi_bancaria_2026            22 fragmentos
  regulatorio_bancario_2026     51 fragmentos
  tributaria_bolivia_2026       80 fragmentos

Consultas aleatorias por coleccion: 40   Semilla fija: 20260929
  asfi_bancaria_2026            OK
  regulatorio_bancario_2026     OK
  tributaria_bolivia_2026       OK
  Coleccion inexistente: 0 resultados (esperado 0)

Filas recuperadas en total : 2400
Filas de otra coleccion   : 0
[OK] CERO CONTAMINACION CRUZADA CONFIRMADA
```

Son 120 consultas de 768 dimensiones con umbral `match_threshold = -1.0` (que
acepta cualquier similitud, para maximizar las posibilidades de fuga). Ninguna
devolvió una fila de otra colección, y una colección inexistente devuelve vacío
en lugar de degradar a una búsqueda global.

**Verificación semántica, con el LLM** (preguntas cruzadas sobre `/api/consultar`):

| Prueba | Colección consultada | Fuentes recuperadas | Resultado |
|---|---|---|---|
| Pregunta **tributaria** (impuesto sobre la renta, IVA) | bancaria | 4/4 de `Sistema_bancario` | LLM: "el contexto no contiene esa información" |
| Pregunta **bancaria** (depósito bancario) | tributaria | 4/4 de `Tributo` / `Impuesto_sobre_la_renta` | LLM: "no existe información" |
| Pregunta **tributaria** correcta | tributaria | `Tributo`, `Sistema_tributario` (sim. 73 %) | Responde con la definición de tributo |
| Pregunta **bancaria** correcta | bancaria | `Intermediario_financiero` (sim. 85 %) | Responde la definición |

En ninguno de los dos casos cruzados aparece un fragmento del otro dominio: el
aislamiento se sostiene a nivel de recuperación, no solo de generación.

> Ambas verificaciones requieren cuota de la API solo la segunda. Si la cuota
> diaria está agotada, `/api/consultar` responde
> `Error en búsqueda vectorial: 429 RESOURCE_EXHAUSTED`, mientras que
> `verificar_aislamiento.py` sigue funcionando.

---

## 6. Benchmark de concurrencia (`p = 1, 2, 4, 8`)

Conjunto de prueba: 10 URLs públicas de tamaño comparable (70–220 KB),
`scripts/urls_prueba.txt`.

```bash
# Pipeline completo (Supabase + API)
docker compose exec -T backend python scripts/ingestar_web.py benchmark \
  --urls scripts/urls_prueba.txt --workers 1 2 4 8

# Solo red + CPU, sin llamar a la API (aisla el paralelismo del rate limit)
docker compose exec -T backend python scripts/ingestar_web.py benchmark \
  --urls scripts/urls_prueba.txt --workers 1 2 4 8 --solo-red-cpu

# Regenerar gráficos y tablas
docker compose exec -T backend python scripts/graficos_benchmark.py
```

Resultados y análisis: ver `REPORTE_BENCHMARK.md` y los gráficos
`grafico_speedup.png`, `grafico_desglose_redcpu.png`, `grafico_desglose_completo.png`.

---

## 7. Flujo operativo completo (hoja de ruta)

```bash
# 1. Recarga de credenciales (cuando se editen las claves)
#    editar .env y luego:
docker compose up -d --force-recreate backend frontend

# 2. Descargar normativa del portal ASFI (ya ejecutado: 25 PDFs en documentos_pdf/)
docker compose exec backend python scripts/descargar_asfi.py

# 3. Ingesta vectorial de PDFs (OCR + embeddings + pgvector), 2 núcleos:
docker compose exec backend python scripts/ingest_normativa.py --workers 2

# 4. Ingesta web por colección:
docker compose exec backend python scripts/ingestar_web.py ingerir \
  --urls scripts/urls_bancarias.txt --coleccion regulatorio_bancario_2026 --workers 4

# 5. Monitorear cómputo durante la ingesta (en otra terminal):
docker stats lexbancario_backend

# 6. Usar el asistente:
#    http://localhost:8501   (chat, con selector de colección)
#    http://localhost:8000/docs  (API Swagger)
```

---

## 8. Notas técnicas y advertencias

- `requirements.txt` fue **reescrito**: el original estaba incompleto (faltaban
  fastapi, streamlit, supabase, python-dotenv, PyMuPDF, pytesseract, pdf2image) y
  forzaba `pandas 1.5.3` (sin wheel para Python 3.12) que rompía el build. El
  contenido original quedó conservado como referencia en `requierementsOG` **sin uso**.
- Se agregaron `httpx`, `beautifulsoup4` y `lxml` para el scraper, y `matplotlib`
  para los gráficos del benchmark.
- El contenedor corre como `root`; los archivos que crea dentro de `documentos_pdf/`
  aparecen como `root:root` en el host. Se corrigió con `sudo chown`; si al descargar
  de nuevo ves carpetas sin escribir, repite el `sudo chown -R ellis:ellis documentos_pdf`).
- La cuota diaria de la API de embeddings es el límite real del pipeline: al
  agotarse devuelve `429 RESOURCE_EXHAUSTED` y los reintentos inflan los tiempos de
  wallclock (ver `REPORTE_BENCHMARK.md`).
- Un proyecto de Supabase gratuito se pausa tras una semana de inactividad. Si
  `/api/colecciones` devuelve error de DNS (`NXDOMAIN`), hay que restaurarlo desde
  el panel antes de consultar.
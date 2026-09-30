# Reporte de benchmark — Ingesta web concurrente

**Proyecto:** Sistema RAG ASFI / Banco Unión (`lexbancario-ai`)
**Maquina:** 2 vCPU
**Conjunto de prueba:** 10 URLs públicas de tamaño comparable (70–220 KB HTML)
**Fases medidas:** descarga (red), limpieza + particionado (CPU), embeddings (API externa), inserción (BD)

---

## 1. Metodología

### 1.1 Por qué dos mediciones

El enunciado pide comparar `T_s` contra `T_p` y justificar si la aceleración está
limitada por el cuello de botella de I/O externo. Para separar las dos causas
posibles de una mala aceleración hay que medirlas por separado:

| Medición | Qué aísla | Comando |
|---|---|---|
| **Red + CPU** | El comportamiento del paralelismo, sin la API | `--solo-red-cpu` |
| **Pipeline completo** | El costo real end-to-end, con API y BD | (sin flags) |

Si solo se midiera el pipeline completo, el rate limit de la API enmascararía por
completo el efecto del paralelismo.

### 1.2 Fórmulas

- **Speedup:** `S_p = T_1 / T_p`
- **Eficiencia:** `E_p = S_p / p`
- **Desglose:** porcentaje de cada fase respecto al wallclock total.

Las fases se miden con un context manager (`Metricas.medir`) que acumula tiempo
real; las etapas son secuenciales entre sí, por lo que el desglose suma 100 %.

### 1.3 Réplicas

Cada punto de la serie controlada es la **media de 3 réplicas independientes**, con
mínimo, máximo y desviación estándar. Esto importa: una medición única dio
`S_8 = 3.73`, y la media de tres réplicas da `S_8 = 2.70`. La primera cifra era una
corrida afortunada, no un resultado reproducible.

La dispersión crece con `p` (coeficiente de variación de `T_p`: 4.4 % en `p=1`,
10.1 % en `p=2`, 14.1 % en `p=4`, 22.9 % en `p=8`). Tiene sentido: con más
concurrentes hay más respuestas de red superpuestas y más variabilidad en el
momento de arranque de los procesos. Por eso los valores de `p=8` se reportan
siempre con su rango.

La serie end-to-end **no** tiene réplicas, porque solo `p=1` es válida (ver 2.2) y
no hay réplicas válidas que promediar.

---

## 2. Resultados

### 2.1 Red + CPU (controlado, sin llamada a la API)

| p | T_p media (s) | desv | rango | S_p media | rango S_p | E_p | scraping | chunking |
|---|---|---|---|---|---|---|---|---|
| 1 | 7.70 | 0.34 | 7.50–8.09 | 1.00 | — | 1.00 | 92.5 % | 7.5 % |
| 2 | 4.83 | 0.49 | 4.39–5.36 | **1.60** | 1.40–1.71 | 0.80 | 87.5 % | 12.5 % |
| 4 | 3.34 | 0.47 | 3.03–3.88 | **2.33** | 2.09–2.48 | 0.58 | 80.4 % | 19.6 % |
| 8 | 2.97 | 0.68 | 2.18–3.37 | **2.70** | 2.23–3.45 | 0.34 | 71.5 % | 28.5 % |

T absolutos por fase (descontados del wallclock medio):

| p | descarga (s) | CPU limpieza+chunking (s) | speedup de la descarga |
|---|---|---|---|
| 1 | 7.12 | 0.58 | 1.00x |
| 2 | 4.23 | 0.60 | 1.69x |
| 4 | 2.68 | 0.66 | 2.65x |
| 8 | 2.12 | 0.85 | 3.35x |

### 2.2 Pipeline completo (con Supabase y API)

| p | T_p (s) | S_p | E_p | scraping | chunking | embeddings | inserción |
|---|---|---|---|---|---|---|---|
| 1 | 81.65 | 1.00 | 1.00 | 9.4 % | 0.8 % | 75.2 % | 14.6 % |
| 2 | 157.74 | 0.52 | 0.26 | 2.2 % | 0.4 % | 91.9 % | 5.6 % |
| 4 | 551.86 | 0.15 | 0.04 | 0.4 % | 0.1 % | 99.5 % | 0.0 % |
| 8 | 554.07 | 0.15 | 0.02 | 0.3 % | 0.1 % | 99.6 % | 0.0 % |

⚠️ **Solo `p=1` es una medición válida.** En `p>=2` se agotó la cuota diaria de la
API de embeddings (`429 RESOURCE_EXHAUSTED`) y los reintentos con backoff
(2 s → 60 s) inflaron el wallclock. Los valores `S_p < 1` de esa tabla **no
miden el paralelismo**: miden tiempo de espera por cuota.

---

## 3. Análisis

### 3.1 El paralelismo funciona: 3.35x en la fase de red, 2.70x global

Dos cifras distintas que conviene no confundir:

- La **fase de descarga** pasa de 7.12 s a 2.12 s → `3.35x`.
- El **conjunto Red + CPU** pasa de 7.70 s a 2.97 s → `2.70x` de media.

La descarga es la fase que domina el tiempo en la medición controlada (92.5 % en
`p=1`) y es la que escala con `p`. La cifra global es menor porque la fase de CPU
no escala, como se ve en 3.2.

`S_4 = 2.33` supera el número de núcleos (2). No es una contradicción: la fase
dominante es espera de red, no cómputo. Los 2 vCPU solo limitan la fase de CPU, y
mientras los hilos esperan respuesta HTTP los núcleos pueden atender otras
peticiones. El límite de esta fase lo impone la latencia acumulada de los
servidores de destino, no el hardware del host. Conviene matizarlo: el dato es
compatible con un `S_p` por encima del número de núcleos, pero no se midió
directamente el porcentaje de ocupación de CPU durante la ejecución, así que la
explicación es la coherente con la evidencia disponible y no una medición de
ocupación.

### 3.2 La fase de CPU no escala, y hay evidencia directa de por qué

El tiempo de limpieza + particionado **no baja, sube levemente**:

```
p=1: 0.58 s   p=2: 0.60 s   p=4: 0.66 s   p=8: 0.85 s
```

Con solo 10 documentos, cada proceso trabajador recibe 1–2 documentos. La
hipótesis más simple es que el costo de crear, distribuir y destruir procesos
(`ProcessPoolExecutor` hace `fork` y serializa el HTML por `pickle`) supera el
ahorro de cómputo.

Para no quedarme en una hipótesis, medí la fase de particionado de forma aislada
(8 documentos de ~182 000 caracteres, sin red ni API), leyendo CPU real con
`os.times()`:

| workers | wall (s) | cpu (s) | cpu/wall | speedup |
|---|---|---|---|---|
| 1 | 0.01 | 0.01 | 1.16 | 1.00x |
| 2 | 0.05 | 0.02 | 0.44 | 0.19x |
| 4 | 0.07 | 0.01 | 0.15 | 0.13x |
| 8 | 0.11 | 0.04 | 0.36 | 0.08x |

Con este volumen, **más workers es más lento**: el overhead domina por completo.
`cpu/wall` queda muy por debajo del techo teórico de 2.0 que impone el contenedor
(2 vCPU visibles). Esto no es una prueba de saturación de CPU —es lo contrario—
sino evidencia de que la fase de CPU de este pipeline es **overhead-dominated** y
no intensively paralelizable tal como está particionada.

**Extensión natural:** partir el particionado por *página* o por *bloque de
fragmentos* en lugar de por documento completo, para generar una cola de tareas
más fina y que el costo de `fork` se amortice. Alternativamente, un pool
persistente en lugar de uno creado por tanda.

### 3.3 Amdahl como chequeo de consistencia, no como techo

En `p=8` la composición observada es 71.5 % red y 28.5 % CPU. Aplicando la Ley de
Amdahl con esa composición:

```
S_amdahl ≈ 1 / (0.285 + 0.715/8) ≈ 2.67
S_medido = 2.70
```

El modelo predice `2.67` y se midió `2.70`: coinciden dentro del 1.1 %. Es un buen
chequeo de consistencia, y esa es exactamente su función.

**No es un techo del sistema**, por dos razones:

1. La fracción "serial" **cambia con `p`** (7.5 % → 12.5 % → 19.6 % → 28.5 %), así
   que el valor de la derecha se recalcula para cada `p`. Aplicado a los cuatro
   puntos predice 1.78, 2.52, 2.67 frente a los medidos 1.60, 2.33, 2.70: siempre
   ligeramente por encima, como corresponde cuando el término "serial" en realidad
   también tiene costo variable con `p`.
2. La parte no serial escala *mejor* que `p` cuando es I/O-bound, supuesto que
   Amdahl no modela.

### 3.4 El cuello de botella real: la API de embeddings

En el pipeline completo los embeddings pasan del **75 % al 99.6 %** del tiempo.
Bajo las condiciones observadas, la API de embeddings es el factor limitante, y la
concurrencia adicional no mejora el resultado: con `p>=2` el tiempo *crece* hasta
554 s frente a 81.65 s en `p=1`.

**No corresponde aplicar aquí Amdahl con una fracción serial de 0.75 para
obtener un "techo" de `1.33x`.** La fase de embeddings es I/O-bound y
conceptualmente paralelizable; lo que se agotó fue la **cuota de la API**, un
límite de recursos compartidos con el resto de clientes, no una sección
estrictamente serial del programa. Amdahl modela fracciones de código que no se
pueden paralelizar, no cuotas de terceros: usar sus fórmulas aquí produce un
número sin interpretación física.

Lo que sí se puede afirmar, con los datos en la mano:

- Bajo las condiciones medidas, la API de embeddings es el factor limitante.
- El recurso no se comporta como serial sino como **conmutable**: al agotarse la
  cuota, el backoff (2 s → 60 s) convierte cada `p` adicional en más lotes
  esperando, y el wallclock crece.
- Cuantificar el techo real requiere medir el comportamiento de la API dentro de
  su cuota disponible, lo cual no fue posible porque la cuota ya estaba agotada
  al momento de medir. Con `p=1` y cuota sana, los embeddings son el 75.2 % del
  tiempo.

### 3.5 Aislamiento multi-tenant

Verificación reproducible con `scripts/verificar_aislamiento.py`: **40 consultas
por cada una de 3 colecciones, 120 consultas en total**, todas con
`match_threshold = -1.0` (para que el filtro de similitud no pueda discriminar por
sí solo) y `match_count = 20`. Resultado: **2400 filas recuperadas, 0 filas
pertenecientes a otra colección**. Las 120 consultas se generaron con
combinaciones aleatorias de `ole_date`, `sancion`, `medida` y `pais`, y los tres
embeddings de consulta son todos de una colección distinta para que la búsqueda
por similitud no favorezca a la colección de la query.

### 3.6 Índice vectorial: verificado con `EXPLAIN ANALYZE`

El esquema crea un índice **HNSW** (`vector_cosine_ops`) sobre `embedding` y un
**B-Tree** sobre `coleccion_id` (ver `scripts/schema.sql`). Se ejecutó
`EXPLAIN (ANALYZE, BUFFERS)` para comprobar qué elige realmente el planificador.
Plan obtenido:

```
Limit  (actual time=5.336..5.342 rows=20 loops=1)
  Buffers: shared hit=1494
  ->  Sort  (actual time=5.335..5.337 rows=20 loops=1)
        Sort Key: ((n.embedding <=> $query))
        Sort Method: top-N heapsort  Memory: 27kB
        Buffers: shared hit=1494
        ->  Index Scan using idx_normativa_coleccion_btree on normativa_bancaria n
              (actual time=1.715..5.232 rows=80 loops=1)
              Index Cond: ((coleccion_id)::text = 'tributaria_bolivia_2026'::text)
              Filter: ((embedding IS NOT NULL) AND ((1 - (embedding <=> $query)) > 0.3))
              Buffers: shared hit=1491
Planning Time: 1.626 ms
Execution Time: 5.482 ms
```

Dos lecturas, y la segunda es la importante:

1. **El B-Tree sí se usa**, como `Index Cond` sobre `coleccion_id`. Confirma que
   es la pieza que hace barato el aislamiento por tenant. Sigue sin ser
   *indispensable*: sin él el plan sería un seq scan sobre las 153 filas totales en
   lugar de 80, una diferencia irrelevante a esta escala.
2. **El índice HNSW no aparece en el plan.** El planificador eligió B-Tree por
   colección + filtro + `top-N heapsort`, es decir una **búsqueda exacta**. A 22–80
   filas por colección, ordenar 80 vectores es más barato que recorrer un grafo
   HNSW, y con razón: el índice está ahí pero no es rentable todavía.

En esta escala, entonces, el diseño es correcto y rápido (5.5 ms) **gracias al
B-Tree, no al HNSW**. Afirmar que el HNSW acelera la búsqueda habría sido falso.

**Dos salvedades sobre este plan:**

- Es una medición a escala de decenas de filas. A 10 000+ filas por colección el
  planificador probablemente elegirá HNSW, porque el `Sort` dejaría de ser viable.
  Conviene repetir el `EXPLAIN` cuando el corpus crezca.
- El planificador **estimó 27 filas y encontró 80** para el filtro de colección, lo
  que sugiere estadísticas desactualizadas. Un `ANALYZE normativa_bancaria;` es
  barato y evitaría estimaciones de cardinalidad erróneas a mayor escala.

> Nota sobre el plan transcrito: los tres `InitPlan` que aparecen en la salida
> original son un artefacto de la consulta de prueba, que repetía la subconsulta
> del vector de consulta tres veces. La RPC real recibe ese vector como un único
> parámetro, por lo que genera un solo `InitPlan`. La forma del plan (B-Tree +
> sort) es la misma.

### 3.7 Conclusión

1. La concurrencia está bien implementada: `ThreadPoolExecutor` para I/O,
   `ProcessPoolExecutor` para CPU, y la limpieza HTML dentro del
   proceso-trabajador (si estuviera en el hilo principal, la medición de `chunking`
   sería falsa).
2. En la porción controlable del pipeline, el paralelismo aporta **1.60x (p=2),
   2.33x (p=4) y 2.70x (p=8)**, con eficiencia decreciente (`E_8 = 0.34`). La
   eficiencia cae por dos razones medidas: la fase de CPU es overhead-dominated y
   la fase de red escala peor que linealmente a partir de `p=4`.
3. En el pipeline completo, bajo las condiciones observadas, **la API de embeddings
   es el factor limitante** y la concurrencia adicional empeora el resultado. Esto
   es un límite de cuota de un tercero, no un defecto de diseño del paralelismo.
4. La conclusión es la que corresponde a un diseño sensato: **más concurrencia no
   es la respuesta** mientras la fase dominante sea una llamada remota con cuota.
   Las palancas reales serían un modelo de embeddings local, cachés de vectores o
   elevar el paralelismo de embeddings hasta donde llegue la cuota disponible.
5. El cuello de botella con muchas réplicas **sí es medible y corregible**: la
   fase de particionado en proceso. Partir por página en lugar de por documento
   es la mejora de mayor impacto identificado.
6. El índice vectorial está verificado: a escala actual el plan usa el B-Tree de
   `coleccion_id` y búsqueda exacta por `sort`; el HNSW no interviene todavía.
   Reevaluar el plan cuando el corpus crezca.

---

## 4. Artefactos

| Archivo | Contenido |
|---|---|
| `resultados_benchmark_red_cpu.json` | Serie controlada, p={1,2,4,8}, media de 3 réplicas con min/max/desv |
| `resultados_replicas_resumen.json` | Estadísticas de réplica por punto (T y S por réplica) |
| `resultados_replica_{1,2,3}.json` | Cada réplica individual, para auditar el agregado |
| `resultados_benchmark_completo.json` | Medición end-to-end (con nota de validez por fila) |
| `grafico_speedup.png` | `S_p` por p: red+CPU con barras de error, ideal, pipeline completo |
| `grafico_desglose_redcpu.png` | Desglose porcentual por fase, medición controlada |
| `grafico_desglose_completo.png` | Desglose porcentual por fase, pipeline completo |
| `scripts/graficos_benchmark.py` | Genera gráficos y tablas desde los JSON |
| `scripts/urls_prueba.txt` | Las 10 URLs del conjunto de prueba |
| `scripts/urls_bancarias.txt` / `urls_tributarias.txt` | Corpus de las 2 colecciones de aislamiento |

### Reproducir

```bash
# Serie controlada, 3 réplicas por punto
for r in 1 2 3; do
  docker compose exec -T backend python scripts/ingestar_web.py benchmark \
    --urls scripts/urls_prueba.txt --workers 1 2 4 8 --solo-red-cpu
  cp resultados_benchmark.json "resultados_replica_$r.json"
done

# Pipeline completo (requiere cuota disponible)
docker compose exec -T backend python scripts/ingestar_web.py benchmark \
  --urls scripts/urls_prueba.txt --workers 1 2 4 8

docker compose exec -T backend python scripts/graficos_benchmark.py
```

> El benchmark end-to-end requiere cuota disponible en la API de embeddings. Si
> devuelve `429` desde el primer lote, la medición no es utilizable: esperar al
> reinicio de la cuota o evaluar el modo `--solo-red-cpu`.

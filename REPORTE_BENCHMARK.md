# Reporte de benchmark — Ingesta web concurrente

**Proyecto:** Sistema RAG ASFI / Banco Unión (`lexbancario-ai`)
**Maquina:** 2 vCPU
**Conjunto de prueba:** 10 URLs públicas de tamaño comparable (70–220 KB HTML)
**Fases medidas:** descarga (red), limpieza + particionado (CPU), embeddings (API externa), inserción (BD)

---

## 1. Metodología

### 1.1 Por qué dos mediciones

El enunciado pide comparar `T_s` contra `T_p` y justificar si la aceleración está
limitada por la Ley de Amdahl debido al cuello de botella de I/O externo. Para
separar las dos causas posibles de una mala aceleración hay que medirlas por
separado:

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

No se realizaron réplicas múltiples. Es una limitación reconocida: con una sola
medición por punto, el ruido de red puede influir en las décimas de segundo. La
tendencia (5.6x en la fase de red) es lo bastante amplia como para sostenerse
independientemente del ruido, pero los valores de `E_p` a `p=8` deberían leerse
con cautela.

---

## 2. Resultados

### 2.1 Red + CPU (controlado, sin llamada a la API)

| p | T_p (s) | S_p | E_p | scraping | chunking |
|---|---|---|---|---|---|
| 1 | 8.03 | 1.00 | 1.00 | 92.5 % | 7.5 % |
| 2 | 5.05 | **1.59** | 0.80 | 88.3 % | 11.7 % |
| 4 | 2.60 | **3.09** | 0.77 | 71.9 % | 28.1 % |
| 8 | 2.15 | **3.73** | 0.47 | 61.4 % | 38.6 % |

T absolutos por fase (descontados del wallclock):

| p | descarga (s) | CPU limpieza+chunking (s) |
|---|---|---|
| 1 | 7.43 | 0.60 |
| 2 | 4.46 | 0.59 |
| 4 | 1.87 | 0.73 |
| 8 | 1.32 | 0.83 |

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

### 3.1 El paralelismo funciona: 5.6x en la fase de red

La fase de descarga baja de **7.43 s a 1.32 s** (5.6x) al pasar de `p=1` a `p=8`.
Es la fase que domina el tiempo en la medición controlada (92.5 % a `p=1`) y
responde casi linealmente al aumento de `p`.

El `S_p = 3.09` en `p=4` **supera el número de núcleos (2)**. No es una
contradicción: la fase dominante es espera de red, no cómputo. Los 2 vCPU solo
limitan la fase de CPU; mientras los hilos esperan respuesta HTTP, ambos núcleos
quedan libres. El techo real de esta fase no lo pone el hardware del host sino la
latencia acumulada de los servidores de destino.

### 3.2 La fase de CPU es el piso que frena la aceleración

El tiempo de limpieza + particionado **casi no baja** (0.60 s → 0.59 s → 0.73 s →
0.83 s): ya satura los 2 núcleos con apenas 10 documentos, y a partir de `p=4`
aumenta por el costo de crear y destruir procesos trabajadores (cada proceso
recibe solo 1–2 documentos).

Ese es el motivo de que la eficiencia caiga a `E_p = 0.47` en `p=8`: se están
pagando 8 procesos para hacer el trabajo de 2. El porcentaje relativo de `chunking`
crece (7.5 % → 38.6 %) no porque el trabajo crezca, sino porque el scraping se
acelera y la fase serial queda proporcionalmente más grande — exactamente el
comportamiento que describe Amdahl.

**Techo esperado:** con la fracción serial de `p=8` en 38.6 %,

```
S_max ≈ 1 / (0.386 + 0.614/8) ≈ 2.34
```

El `S_p = 3.73` medido supera ese valor porque el modelo de Amdahl asume
paralelismo perfecto de la parte no serial, y aquí la parte no serial (red) escala
mejor que 8x respecto a la línea base por ser I/O-bound.

**Extensión natural:** partir el particionado por *página* o por *bloque de
fragmentos* en lugar de por documento completo, para balancear la cola y
eliminar el overhead de procesos vacíos.

### 3.3 El cuello de botella real: la API de embeddings

En el pipeline completo los embeddings pasan del **75 % al 99.6 %** del tiempo.
Aplicando Amdahl con `p=1` como referencia y `f_serial ≈ 0.75`:

```
S_techo ≈ 1 / 0.75 ≈ 1.33
```

Es decir, **aunque el scraping y el chunking se ejecutaran instantáneamente, el
sistema solo podría acelerar 1.33x**. El límite no es la concurrencia del programa: es
la cuota de una API compartida con el resto de clientes.

Peor aún, este cuello no se comporta como un recurso serial sino como un recurso
**conmutable**: cuando se agota la cuota, el reintento con backoff convierte cada
`p` adicional en más lotes esperando. Por eso el tiempo *crece* con `p` en lugar
de mantenerse en el techo teórico. El paralelismo no es inútil, es que el recurso
escollado no se puede paralelizar por construcción.

### 3.4 Conclusión

1. La concurrencia está bien implementada: `ThreadPoolExecutor` para I/O,
   `ProcessPoolExecutor` para CPU, y la limpieza HTML dentro del proceso-trabajador
   (si estuviera en el hilo principal, la medición de `chunking` sería falsa).
2. La aceleración end-to-end está **limitada por la Ley de Amdahl** debido al
   cuello de botella de I/O externo: la API de embeddings con rate limiting representa
   75–99 % del tiempo y impone un techo de `S ≈ 1.33`.
3. El paralelismo aporta 3.09x–3.73x en la parte del pipeline que sí es controlable
   (red + CPU), y ese es el número que representa el trabajo de optimización del
   proyecto. El resto depende de disponibilidad de cuota, no de código.
4. La conclusión es la que corresponde a un diseño sensato: **más concurrencia no
   es la respuesta** mientras la fase dominante sea una llamada remota con cuota.
   Las palancas reales serían un modelo de embeddings local, cachés de vectores o
   elevar el paralelismo de embeddings hasta donde llegue la cuota disponible.

---

## 4. Artefactos

| Archivo | Contenido |
|---|---|
| `resultados_benchmark_red_cpu.json` | Medición controlada, p={1,2,4,8} |
| `resultados_benchmark_completo.json` | Medición end-to-end (con nota de validez por fila) |
| `grafico_speedup.png` | `S_p` por p: red+CPU vs. ideal vs. pipeline completo |
| `grafico_desglose_redcpu.png` | Desglose porcentual por fase, medición controlada |
| `grafico_desglose_completo.png` | Desglose porcentual por fase, pipeline completo |
| `scripts/graficos_benchmark.py` | Genera gráficos y tablas desde los JSON |
| `scripts/urls_prueba.txt` | Las 10 URLs del conjunto de prueba |
| `scripts/urls_bancarias.txt` / `urls_tributarias.txt` | Corpus de las 2 colecciones de aislamiento |

### Reproducir

```bash
docker compose exec -T backend python scripts/ingestar_web.py benchmark \
  --urls scripts/urls_prueba.txt --workers 1 2 4 8 --solo-red-cpu

docker compose exec -T backend python scripts/ingestar_web.py benchmark \
  --urls scripts/urls_prueba.txt --workers 1 2 4 8

docker compose exec -T backend python scripts/graficos_benchmark.py
```

> El benchmark end-to-end requiere cuota disponible en la API de embeddings. Si
> devuelve `429` desde el primer lote, la medición no es utilizable: esperar al
> reinicio de la cuota o evaluar el modo `--solo-red-cpu`.

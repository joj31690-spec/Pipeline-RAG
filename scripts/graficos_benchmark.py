"""Genera los graficos del benchmark de concurrencia.

Produce:
  grafico_desglose_*.png   Desglose temporal porcentual por fase y por p.
  grafico_speedup.png      Speedup y eficiencia vs. p, con la serie ideal S=p.
"""
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

FASES = ["scraping", "chunking", "embeddings", "insercion"]
ETIQUETAS = ["Scraping (red)", "Chunking (CPU)", "Embeddings (API)", "Insercion (BD)"]
COLORES = ["#2E86AB", "#F6AE2D", "#C44536", "#5B8C5A"]


def cargar(ruta):
    with open(ruta) as fh:
        return json.load(fh)


def desglose(datos):
    """Devuelve (workers, matriz de porcentajes por fase)."""
    filas = datos["filas"]
    workers = [f["workers"] for f in filas]
    matriz = [[f["desglose_pct"].get(fase, 0.0) for fase in FASES] for f in filas]
    return workers, matriz


def grafico_desglose(workers, matriz, titulo, salida):
    """Barras apiladas: una por p, descompuesta por fase."""
    fig, ejes = plt.subplots(figsize=(9, 5.2))
    ancho = 0.6
    base = [0.0] * len(workers)

    for i, fase in enumerate(FASES):
        valores = [fila[i] for fila in matriz]
        ejes.bar(workers, valores, ancho,
                 bottom=base, label=ETIQUETAS[i], color=COLORES[i],
                 edgecolor="white", linewidth=0.8)
        base = [b + v for b, v in zip(base, valores)]

    ejes.set_title(titulo, fontsize=11, pad=12)
    ejes.set_xlabel("Workers (p)")
    ejes.set_ylabel("% del tiempo total (wallclock)")
    ejes.set_xticks(workers)
    ejes.set_ylim(0, 100)
    ejes.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=4, frameon=False)
    ejes.grid(axis="y", alpha=0.25, linewidth=0.6)
    ejes.set_axisbelow(True)
    for lado in ("top", "right"):
        ejes.spines[lado].set_visible(False)
    fig.tight_layout()
    fig.savefig(salida, dpi=150, bbox_inches="tight")
    print(f"[OK] {salida}")


def grafico_speedup(red_cpu, completo, salida="grafico_speedup.png"):
    """Barras agrupadas de speedup con la serie ideal S=p como referencia."""
    fig, ejes = plt.subplots(figsize=(9, 5.2))
    ancho = 0.22
    p_red = [f["workers"] for f in red_cpu["filas"]]
    s_red = [f["speedup"] for f in red_cpu["filas"]]
    p_com = [f["workers"] for f in completo["filas"]]
    s_com = [f["speedup"] for f in completo["filas"]]
    ideal = [p for p in p_red]

    pos = range(len(p_red))
    ejes.bar([i - ancho for i in pos], s_red, ancho,
             label="Red + CPU (controlado)", color="#2E86AB", edgecolor="white")
    ejes.bar(list(pos), ideal, ancho,
             label="Ideal S=p", color="#B8B8B8", edgecolor="white")
    ejes.bar([i + ancho for i in pos], s_com, ancho,
             label="Pipeline completo (429)", color="#C44536", edgecolor="white")

    for i, v in enumerate(s_red):
        ejes.text(i - ancho, v + 0.08, f"{v:.2f}", ha="center", fontsize=8)
    for i, v in enumerate(s_com):
        ejes.text(i + ancho, v + 0.08, f"{v:.2f}", ha="center", fontsize=8)

    ejes.axhline(1.0, color="#333333", linewidth=0.9, linestyle="--", alpha=0.7)
    ejes.text(len(pos) - 0.5, 1.05, "sin ganancia", fontsize=8, color="#333333")
    ejes.set_xlabel("Workers (p)")
    ejes.set_ylabel("Speedup  S_p = T_1 / T_p")
    ejes.set_xticks(list(pos))
    ejes.set_xticklabels([f"p={p}" for p in p_red])
    ejes.legend(frameon=False)
    ejes.grid(axis="y", alpha=0.25, linewidth=0.6)
    ejes.set_axisbelow(True)
    for lado in ("top", "right"):
        ejes.spines[lado].set_visible(False)
    fig.tight_layout()
    fig.savefig(salida, dpi=150, bbox_inches="tight")
    print(f"[OK] {salida}")


def tabla_markdown(red_cpu, completo):
    """Tabla comparativa lista para pegar en el reporte."""
    print("\n### Red + CPU (controlado, sin API)\n")
    print("| p | T_p (s) | S_p | E_p |")
    print("|---|---|---|---|")
    for f in red_cpu["filas"]:
        print(f"| {f['workers']} | {f['T_s']:.2f} | {f['speedup']:.2f} | {f['eficiencia']:.2f} |")
    print("\n### Pipeline completo (con API, 429)\n")
    print("| p | T_p (s) | S_p | E_p | valido |")
    print("|---|---|---|---|---|")
    for f in completo["filas"]:
        ok = "si" if f.get("valido") else "no (cuota 429)"
        print(f"| {f['workers']} | {f['T_s']:.2f} | {f['speedup']:.2f} | "
              f"{f['eficiencia']:.2f} | {ok} |")


def main():
    red_cpu = cargar("resultados_benchmark_red_cpu.json")
    completo = cargar("resultados_benchmark_completo.json")
    grafico_desglose(*desglose(red_cpu),
                     "Desglose temporal: scraping + chunking (controlado, sin API)",
                     "grafico_desglose_redcpu.png")
    grafico_desglose(*desglose(completo),
                     "Desglose temporal: pipeline completo (API + BD)",
                     "grafico_desglose_completo.png")
    grafico_speedup(red_cpu, completo)
    tabla_markdown(red_cpu, completo)


if __name__ == "__main__":
    main()

"""Evaluación del parte contra la verdad de campo de la planta (/api/verdad).

Usa lo que deja una corrida del corrector (`correccion/corridas/<alumno>/` en el repo de la planta): de cada
escenario, el JSON con la verdad de campo y lo que publicó el sistema, y el log del sistema con la
transcripción de cada nota. Mide:

- aciertos por campo del parte que arma cada extractor (reglas, modelo de lenguaje, mixto) sobre las
  transcripciones reales: equipo y variable, valor (con la tolerancia del operador virtual) y unidad;
- si el sistema preguntó solo en las notas que tenían una ambigüedad (lo que mandó a la tablet en la corrida);
- el tiempo de cada extractor y, para el modelo, los tokens.

    python evaluacion/parte.py
    python evaluacion/parte.py --llm http://localhost:11435 --modelo qwen3.5:2b

Deja el resultado en evaluacion/resultados_parte.md.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

from sistema.equipos import A_KG, Catalogo
from sistema.extractor import extraer
from sistema.llm import ExtractorLLM, a_filas

AQUI = Path(__file__).resolve().parent
CORRIDAS = AQUI.parent.parent / "academia2026-tp" / "correccion" / "corridas" / "marcelo-illanes"
ESCENARIOS = ("martes-tranquilo", "fuga-sin-enlace")
_TRANSCRIPCION = re.compile(r"(?P<id>[A-Z]+-\d+) \([\d.]+ s de audio, transcripta a los [\d.]+ s\)(?: ALERTA \S+ a los [\d.]+ s)?: (?P<texto>.*)$")


def cerca(a: float | None, b: float | None, ua: str | None, ub: str | None) -> bool:
    """La misma tolerancia que usa el operador virtual para confirmar un valor."""
    if a is None or b is None:
        return False
    if ua in A_KG and ub in A_KG:
        a, b = a * A_KG[ua], b * A_KG[ub]
        return abs(a - b) <= max(0.6, abs(b) * 0.02)
    return abs(a - b) <= max(0.051, abs(b) * 0.015)


def cargar(carpeta: Path) -> list[dict]:
    """Las notas de las corridas: transcripción del log y verdad de campo del JSON."""
    notas = []
    for escenario in ESCENARIOS:
        log, datos = carpeta / f"{escenario}.log", carpeta / f"{escenario}.json"
        if not log.exists() or not datos.exists():
            continue
        textos = {}
        for linea in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if m := _TRANSCRIPCION.search(linea):
                textos[m["id"]] = m["texto"].strip()
        corrida = json.loads(datos.read_text(encoding="utf-8"))
        tablet = [r["payload"] for r in corrida["registro"] if r["topico"] == "sistema/tablet" and r["valido"]]
        for v in corrida["verdad"]:
            if "recorrida_id" not in v or v.get("tipo") == "vivo" or v["nota_id"] not in textos:
                continue
            mensajes = [m for m in tablet if v["nota_id"] in m.get("nota_ids", [])]
            pregunto = any(m.get("espera_respuesta", True) and (not (m.get("pantalla") or {}).get("filas")
                                                               or (m.get("pantalla") or {}).get("opciones"))
                           for m in mensajes)
            notas.append({"escenario": escenario, "nota_id": v["nota_id"], "transcripcion": textos[v["nota_id"]],
                          "dicho": v["texto"], "items": v["items"], "ambigua": bool(v.get("ambiguedades")),
                          "segura": not v.get("seguridad"), "pregunto": pregunto})
    return notas


def comparar(filas: list, items: list[dict]) -> dict[str, int]:
    """Aciertos por campo de las filas contra los ítems con número de la verdad (sin el goteo, que el
    operador no dice en voz alta: lo informa cuando corrige)."""
    esperados = [i for i in items if i.get("valor") is not None and i["variable"] != "goteo_empaquetadura"]
    c = {"esperados": len(esperados), "equipo": 0, "valor": 0, "unidad": 0, "sobran": 0}
    usadas = set()
    for e in esperados:
        f = next((f for k, f in enumerate(filas) if k not in usadas and f.tag == e["tag"]
                  and f.variable == e["variable"] and f.valor is not None), None)
        if f is None:
            continue
        usadas.add(filas.index(f))
        c["equipo"] += 1
        c["valor"] += cerca(f.valor, e["valor"], f.unidad, e.get("unidad"))
        c["unidad"] += f.unidad == e.get("unidad")
    c["sobran"] = sum(1 for k, f in enumerate(filas) if f.valor is not None and k not in usadas
                      and f.variable != "goteo_empaquetadura")
    return c


def evaluar(notas: list[dict], catalogo: Catalogo, llm: ExtractorLLM | None) -> dict[str, dict]:
    extractores = {"reglas": lambda t: extraer(t, catalogo)}
    if llm:
        def modelo(texto: str) -> list:
            filas = llm.extraer(texto)
            return a_filas(filas, catalogo) if filas is not None else []
        extractores["modelo"] = modelo
    resultados = {}
    for nombre, extractor in extractores.items():
        total = {"esperados": 0, "equipo": 0, "valor": 0, "unidad": 0, "sobran": 0}
        tiempos, ambiguas_ok, notas_ok = [], 0, 0
        for n in notas:
            t0 = time.perf_counter()
            filas = extractor(n["transcripcion"])
            tiempos.append(time.perf_counter() - t0)
            c = comparar(filas, n["items"])
            for k in total:
                total[k] += c[k]
            notas_ok += c["valor"] == c["esperados"] and c["sobran"] == 0
            ambiguas_ok += any(f.ambigua for f in filas) == n["ambigua"]
        resultados[nombre] = total | {"notas": len(notas), "notas_perfectas": notas_ok, "ambiguedad_ok": ambiguas_ok,
                                      "segundos_medio": statistics.mean(tiempos), "segundos_max": max(tiempos)}
    return resultados


def informe(notas: list[dict], resultados: dict[str, dict], modelo: str | None) -> str:
    def pct(a: int, b: int) -> str:
        return f"{100 * a / b:.0f} %" if b else "–"

    lineas = ["# Evaluación del parte contra la verdad de campo", "",
              f"{len(notas)} notas de las corridas del corrector ({', '.join(ESCENARIOS)}), con la transcripción real "
              "de Whisper small. Se comparan las lecturas con número que dijo el operador (sin el goteo, que no "
              "dice en voz alta).", "",
              "| Extractor | Equipo y variable | Valor | Unidad | Lecturas de más | Notas perfectas | Marca bien la ambigüedad | Tiempo medio por nota |",
              "|---|---|---|---|---|---|---|---|"]
    for nombre, r in resultados.items():
        etiqueta = f"{nombre} ({modelo})" if nombre == "modelo" and modelo else nombre
        lineas.append(f"| {etiqueta} | {pct(r['equipo'], r['esperados'])} | {pct(r['valor'], r['esperados'])} | "
                      f"{pct(r['unidad'], r['esperados'])} | {r['sobran']} | {pct(r['notas_perfectas'], r['notas'])} | "
                      f"{pct(r['ambiguedad_ok'], r['notas'])} | {r['segundos_medio'] * 1000:.0f} ms |")
    claras = [n for n in notas if not n["ambigua"] and n["segura"]]
    ambiguas = [n for n in notas if n["ambigua"]]
    lineas += ["", "## ¿Preguntó solo cuando hacía falta?", "",
               f"- Notas con ambigüedad en las que preguntó: {sum(n['pregunto'] for n in ambiguas)} de {len(ambiguas)}.",
               f"- Notas claras en las que preguntó de más: {sum(n['pregunto'] for n in claras)} de {len(claras)}.",
               "", "## Notas", "", "| Escenario | Nota | Lo que dijo | Lo que transcribió Whisper |", "|---|---|---|---|"]
    lineas += [f"| {n['escenario']} | {n['nota_id']} | {n['dicho']} | {n['transcripcion']} |" for n in notas]
    return "\n".join(lineas) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corridas", type=Path, default=CORRIDAS, help="carpeta de la corrida del corrector")
    ap.add_argument("--planta", default="http://localhost:8100", help="API de la planta (para /api/planta)")
    ap.add_argument("--llm", help="URL de Ollama para comparar con el modelo de lenguaje")
    ap.add_argument("--modelo", default="qwen2.5:1.5b")
    ap.add_argument("--salida", type=Path, default=AQUI / "resultados_parte.md")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    notas = cargar(a.corridas)
    if not notas:
        raise SystemExit(f"No hay corridas en {a.corridas}")
    catalogo = Catalogo.desde_api(httpx.get(f"{a.planta}/api/planta", timeout=10).json())
    llm = ExtractorLLM(a.llm, a.modelo, timeout_s=120) if a.llm else None
    texto = informe(notas, evaluar(notas, catalogo, llm), a.modelo if llm else None)
    a.salida.write_text(texto, encoding="utf-8")
    print(texto.split("## Notas")[0])


if __name__ == "__main__":
    main()

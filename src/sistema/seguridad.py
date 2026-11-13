"""Detector local de condiciones inseguras (S1 a S4).

Son reglas en código, sin modelos: tienen que andar sin enlace, tardar milisegundos y dar siempre lo mismo
para el mismo texto. Cada falsa alarma resta, así que cada regla pide algo específico de la condición
("agua a presión", no solo "agua") y se descarta si está negada ("no hay olor a gas", "nadie se lastimó").
Cuando una señal es ambigua ("se cortó" puede ser la luz), pide una parte del cuerpo o una persona en la
misma oración.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .texto import oracion_normalizada, oraciones

DESCRIPCION = {
    "S1": "pérdida con proyección de agua a presión",
    "S2": "olor a gas",
    "S3": "humo o fuego",
    "S4": "persona lesionada",
}

# Palabras que niegan lo que viene enseguida ("no hay humo", "sin olor a gas", "ni fuego"), dentro de la
# misma cláusula y a no más de VENTANA_NEGACION palabras.
NEGADORES = {"no", "sin", "ni", "nadie", "ningun", "ninguna", "ninguno", "nada", "nunca", "tampoco", "jamas"}
VENTANA_NEGACION = 3

_DONDE_ROMPE = r"(?:descarga|cano|caneria|manguera|brida|tuberia)"
_CUERPO = (r"mano|manos|dedo|dedos|brazo|brazos|pierna|piernas|pie|pies|cabeza|ojo|ojos|cara|espalda|rodilla|"
           r"tobillo|muneca|hombro")

_REGLAS = {
    "S1": [
        r"\bagua\s+(?:\w+\s+){0,3}a\s*(?:mucha\s+|alta\s+|toda\s+|pura\s+)?presion",    # sale agua a presión
        r"\bagua\s+(?:\w+\s+){0,3}con\s+(?:mucha|alta|toda|tanta)\s+presion",
        r"\bchorro\w*\s+(?:\w+\s+){0,3}(?:a|con)\s*(?:mucha\s+|alta\s+|toda\s+)?presion",
        r"\b(?:agua|chorro\w*)\s+(?:\w+\s+){0,4}para todos (?:lados|lugares)",
        r"\bproyecc\w*\s+de\s+agua", r"\bproyecta\w*\s+agua",
        rf"\b(?:se rompio|rotura|revento|estallo|se partio|se fisuro)\b(?:\s+\w+){{0,5}}?\s+{_DONDE_ROMPE}\b",
    ],
    "S2": [
        r"\b(?:olor\w*|huele|hiede|apesta)\b(?:\s+\w+){0,3}?\s+gas\b",                   # olor a gas, huele raro a gas
        r"\b(?:fuga|perdida|escape)\s+de\s+gas\b",
        r"\bhuevo podrido\b", r"\b(?:h2s|sh2|sulfhidrico)\b",
    ],
    "S3": [
        r"\bhumo\b", r"\bhumareda\b", r"\bfuego\b", r"\bllamarada\w*\b",
        r"\b(?:en|hay|salen|sale|con|unas)\s+llamas\b",                                  # "me llamás" no es fuego
        r"\b(?:hay|un|principio de|se)\s+(?:un\s+)?incendi\w*",                          # "red de incendio" no
        r"\bolor\s+a\s+quemado\b",
    ],
    "S4": [   # lesiones que no necesitan más contexto
        r"\b(?:herid|lesionad|accidentad|desmayad|desvanecid)[oa]s?\b", r"\binconsciente\b", r"\bno respira\b",
        r"\bse (?:desmayo|desvanecio|accidento|electrocuto)\b", r"\bfractur\w*\b", r"\bquemadura\w*\b",
        r"\bambulancia\b", r"\bme cai\b",
    ],
}
# Lesiones que piden una persona o una parte del cuerpo en la misma oración: "se cortó" puede ser la luz,
# "se me cayó" puede ser la llave y "sangrar" también es purgar una línea.
_S4_VERBOS = [
    r"\b(?:se|me|le)\s+(?:golpe|lastim|cort|quem|cay|machuc|aplast|torc|apret|perfor)\w*",
    r"\bsangr\w*",
]
_S4_CONTEXTO = re.compile(
    rf"\b(?:companer[oa]s?|operador\w*|ayudante|tecnico|persona|alguien|muchach[oa]|chico|pibe|soldador|"
    rf"contratista|{_CUERPO})\b")

_COMPILADAS = {codigo: [re.compile(p) for p in patrones] for codigo, patrones in _REGLAS.items()}
_S4_VERBOS_C = [re.compile(p) for p in _S4_VERBOS]


@dataclass(frozen=True)
class Hallazgo:
    codigo: str
    descripcion: str
    oracion: str   # la oración normalizada donde apareció: es la evidencia


def _negada(oracion: str, cortes: list[int], inicio: int) -> bool:
    desde = max(c for c in cortes if c <= inicio)
    previas = oracion[desde:inicio].split()[-VENTANA_NEGACION:]
    return any(p in NEGADORES for p in previas)


def _aparece(patrones: list[re.Pattern], oracion: str, cortes: list[int]) -> bool:
    return any(not _negada(oracion, cortes, m.start()) for p in patrones for m in p.finditer(oracion))


def detectar(texto: str) -> list[Hallazgo]:
    """Las condiciones inseguras que menciona el texto, una por código, en orden de S1 a S4."""
    hallazgos: dict[str, Hallazgo] = {}
    for original in oraciones(texto):
        oracion, cortes = oracion_normalizada(original)
        if not oracion:
            continue
        for codigo, patrones in _COMPILADAS.items():
            if codigo not in hallazgos and _aparece(patrones, oracion, cortes):
                hallazgos[codigo] = Hallazgo(codigo, DESCRIPCION[codigo], oracion)
        if "S4" not in hallazgos and _S4_CONTEXTO.search(oracion) and _aparece(_S4_VERBOS_C, oracion, cortes):
            hallazgos["S4"] = Hallazgo("S4", DESCRIPCION["S4"], oracion)
    return [hallazgos[c] for c in sorted(hallazgos)]

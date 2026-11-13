"""Números dichos en castellano de campo, como los dice el operador o los escribe Whisper.

    "mil seiscientos cuarenta" -> 1640       "1620" -> 1620          "ciento catorce" -> 114
    "uno coma cuatro", "uno con cuatro", "uno cuatro", "1 coma 4" -> 1.4
    "uno y medio", "1 y medio" -> 1.5        "cero nueve", "09" -> 0.9

Trabaja sobre tokens ya normalizados (minúsculas, sin tildes). `preparar` convierte antes los separadores
decimales y de miles de Whisper ("1,4", "1.640") en algo que sobreviva a la normalización.
"""

from __future__ import annotations

import re

UNIDADES = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21,
    "veintiuno": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26,
    "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
}
DECENAS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80,
           "noventa": 90}
CENTENAS = {"cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200, "trescientos": 300,
            "trescientas": 300, "cuatrocientos": 400, "cuatrocientas": 400, "quinientos": 500, "quinientas": 500,
            "seiscientos": 600, "seiscientas": 600, "setecientos": 700, "setecientas": 700, "ochocientos": 800,
            "ochocientas": 800, "novecientos": 900, "novecientas": 900}
DECIMAL = {"coma", "con", "punto"}
CONECTORES = DECIMAL | {"y", "medio"}


def preparar(texto: str) -> str:
    """Antes de normalizar: "1.640" -> "1640" (miles), "1,4" / "1.4" -> "1 coma 4", "59°" -> "59 grados"."""
    texto = re.sub(r"(?<!\d)(\d{1,3})\.(\d{3})(?!\d)", r"\1\2", texto)
    texto = re.sub(r"(\d)[.,](\d)", r"\1 coma \2", texto)
    return texto.replace("°", " grados ")


def es_numero(token: str) -> bool:
    return token.isdigit() or token in UNIDADES or token in DECENAS or token in CENTENAS or token == "mil"


def _entero(tokens: list[str]) -> int | None:
    """Un entero bien formado, en palabras o dígitos: "mil seiscientos cuarenta", "cincuenta y cinco", "112".
    None si los tokens no forman un entero ("uno tres" no lo es: es un decimal dicho de corrido)."""
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0].isdigit():
        d = tokens[0]
        return None if len(d) > 1 and d.startswith("0") else int(d)
    total, actual, previo = 0, 0, None   # previo: categoría del último token
    for t in tokens:
        if t == "y":
            if previo != "decena":
                return None
            previo = "y"
            continue
        if t == "mil":
            if previo in ("mil",):
                return None
            total += (actual or 1) * 1000
            actual, previo = 0, "mil"
        elif t in CENTENAS:
            if previo not in (None, "mil"):
                return None
            actual += CENTENAS[t]
            previo = "centena"
        elif t in DECENAS:
            if previo not in (None, "mil", "centena"):
                return None
            actual += DECENAS[t]
            previo = "decena"
        elif t in UNIDADES:
            if previo == "y" and UNIDADES[t] > 9:
                return None
            if previo not in (None, "mil", "centena", "y"):
                return None
            actual += UNIDADES[t]
            previo = "unidad"
        elif t.isdigit():
            if previo not in (None, "mil"):
                return None
            actual += int(t)
            previo = "unidad"
        else:
            return None
    if previo == "y":
        return None
    return total + actual


def _cifra(token: str) -> int | None:
    """Un número de una cifra, en palabra o dígito (para decimales dichos de corrido: "uno ocho")."""
    if token.isdigit() and len(token) == 1:
        return int(token)
    valor = UNIDADES.get(token)
    return valor if valor is not None and valor <= 9 else None


def leer(tokens: list[str]) -> float | None:
    """El valor de un número dicho, o None si no se entiende."""
    tokens = list(tokens)
    while tokens and tokens[0] in CONECTORES - {"medio"}:
        tokens.pop(0)
    while tokens and tokens[-1] in DECIMAL | {"y"}:
        tokens.pop()
    if not tokens:
        return None
    # "uno coma cuatro", "cinco con cuatro", "1 coma 4"
    for i, t in enumerate(tokens):
        if t in DECIMAL:
            entero, decimales = _entero(tokens[:i]), _entero(tokens[i + 1:]) if tokens[i + 1:] else None
            if entero is None or decimales is None:
                return None
            return entero + decimales / 10 ** len(str(decimales))
    # "uno y medio", "1 y medio"
    if len(tokens) >= 3 and tokens[-2:] == ["y", "medio"]:
        entero = _entero(tokens[:-2])
        return None if entero is None else entero + 0.5
    if tokens == ["medio"]:
        return 0.5
    # "09" -> 0.9: el cero de adelante dice que es un decimal dicho de corrido ("cero nueve").
    if len(tokens) == 1 and tokens[0].isdigit() and len(tokens[0]) > 1 and tokens[0].startswith("0"):
        return float("0." + tokens[0][1:])
    entero = _entero(tokens)
    if entero is not None:
        return float(entero)
    # "uno ocho", "cero nueve", "1 3": dos cifras sueltas son un decimal dicho de corrido.
    if len(tokens) == 2 and (a := _cifra(tokens[0])) is not None and (b := _cifra(tokens[1])) is not None:
        return a + b / 10
    return None

"""Normalización de texto hablado: minúsculas, sin tildes y sin puntuación, para comparar frases."""

from __future__ import annotations

import re
import unicodedata

_NO_PALABRA = re.compile(r"[^a-z0-9ñ]+")


def normalizar(texto: str) -> str:
    """'¡Atención! Sale agua a presión' -> 'atencion sale agua a presion'. Conserva la ñ."""
    t = unicodedata.normalize("NFKD", (texto or "").lower().replace("ñ", "\0"))
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("\0", "ñ")
    return _NO_PALABRA.sub(" ", t).strip()


def oraciones(texto: str) -> list[str]:
    """Parte el texto en oraciones (por . ! ? ;), sin normalizar."""
    return [o.strip() for o in re.split(r"[.!?;¡¿]+", texto or "") if o.strip()]


def clausulas(texto: str) -> list[str]:
    """Parte una oración en cláusulas, por comas y por "pero", "aunque", "sino". Cada una normalizada."""
    partes = re.split(r"[,:]+|\b(?:pero|aunque|sino)\b", texto or "", flags=re.IGNORECASE)
    return [n for p in partes if (n := normalizar(p))]


def oracion_normalizada(oracion: str) -> tuple[str, list[int]]:
    """La oración normalizada y dónde empieza cada cláusula, para buscar frases que crucen comas
    ("huele raro, como a gas") sin perder hasta dónde llega una negación."""
    partes = clausulas(oracion)
    cortes, posicion = [], 0
    for p in partes:
        cortes.append(posicion)
        posicion += len(p) + 1
    return " ".join(partes), cortes

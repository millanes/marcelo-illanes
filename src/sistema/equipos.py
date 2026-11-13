"""Los equipos de la planta, sus nombres informales ("la dos", "el de la izquierda", "el pulmón") y sus
límites de alarma.

Todo sale de GET /api/planta: el sistema no tiene nombres ni límites escritos en el código (los del manual
Rev. C son otros y están vencidos). "La bomba" a secas no es el nombre de ningún equipo, así que no se
resuelve: el que lo dijo tiene que aclarar cuál.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .texto import normalizar

log = logging.getLogger(__name__)

# Unidades de presión que se dicen en campo, llevadas a kg/cm2.
A_KG = {"kg/cm2": 1.0, "psi": 1 / 14.2233, "bar": 1 / 0.980665}


@dataclass(frozen=True)
class Equipo:
    tag: str
    tipo: str
    nombres: tuple[str, ...]
    unidad_manometro: str | None = None


@dataclass(frozen=True)
class Mencion:
    inicio: int      # posición en el texto normalizado
    fin: int
    dicho: str       # el nombre como se dijo (normalizado)
    tag: str


def _variantes(alias: str) -> set[str]:
    """Cómo puede aparecer un nombre en una frase: "el de la izquierda" también es "al de la izquierda" y
    "del de la izquierda"; "el filtro a" también aparece sin artículo ("filtro a")."""
    salida = {alias}
    if alias.startswith("el "):
        resto = alias[3:]
        salida |= {"al " + resto, "del " + resto}
        if not resto.startswith("de ") and len(resto.split()) <= 2 and resto not in {
                "primario", "secundario", "grueso", "fino", "colector"}:
            salida.add(resto)
    return salida


class Catalogo:
    def __init__(self, equipos: list[Equipo]) -> None:
        self.equipos = {e.tag: e for e in equipos}
        alias: dict[str, str] = {}
        for e in equipos:
            for nombre in (*e.nombres, e.tag):
                for variante in _variantes(normalizar(nombre)):
                    alias.setdefault(variante, e.tag)
        # Los nombres más largos primero: "la de la punta" antes que cualquier pedazo suyo.
        ordenados = sorted(alias, key=len, reverse=True)
        self._alias = alias
        self._patron = re.compile(r"(?<![a-z0-9ñ])(" + "|".join(re.escape(a) for a in ordenados) + r")(?![a-z0-9ñ])")

    @classmethod
    def desde_api(cls, datos: dict) -> Catalogo:
        """Arma el catálogo con la respuesta de GET /api/planta."""
        return cls([Equipo(tag=e["tag"], tipo=e.get("tipo", ""), nombres=tuple(e.get("nombres", [])),
                           unidad_manometro=(e.get("manometro_local") or {}).get("unidad"))
                    for e in datos.get("equipos", [])])

    def menciones(self, texto: str) -> list[Mencion]:
        """Los equipos nombrados en el texto, en el orden en que aparecen."""
        t = normalizar(texto)
        return [Mencion(m.start(), m.end(), m.group(1), self._alias[m.group(1)]) for m in self._patron.finditer(t)]

    def unico(self, texto: str) -> str | None:
        """El tag si el texto nombra exactamente un equipo; si nombra varios o ninguno, None."""
        tags = {m.tag for m in self.menciones(texto)}
        return tags.pop() if len(tags) == 1 else None

    def nombre(self, tag: str) -> str:
        """El primer nombre informal ("la uno"), o el tag si no tiene."""
        e = self.equipos.get(tag)
        return e.nombres[0] if e and e.nombres else tag

    def de_tipo(self, tipo: str) -> list[str]:
        return [t for t, e in self.equipos.items() if e.tipo == tipo]


@dataclass(frozen=True)
class Limite:
    alarma_alta: float | None = None
    alarma_baja: float | None = None


@dataclass
class DatosPlanta:
    catalogo: Catalogo
    limites: dict[tuple[str, str], Limite] = field(default_factory=dict)   # (tag, variable del sensor)

    @classmethod
    def desde_api(cls, datos: dict) -> DatosPlanta:
        limites = {(s["tag"], s["variable"]): Limite(s.get("alarma_alta"), s.get("alarma_baja"))
                   for s in datos.get("sensores", [])}
        return cls(Catalogo.desde_api(datos), limites)


class FuenteDatosPlanta:
    """Lee /api/planta una vez y la guarda. Si la planta no contesta, se reintenta en el próximo pedido."""

    def __init__(self, leer: Callable[[], dict]) -> None:
        self._leer = leer
        self._datos: DatosPlanta | None = None
        self._candado = threading.Lock()

    @property
    def cargados(self) -> DatosPlanta | None:
        """Los datos si ya se leyeron, sin ir a buscarlos (para el hilo de red, que no puede esperar)."""
        return self._datos

    def obtener(self) -> DatosPlanta | None:
        with self._candado:
            if self._datos is None:
                try:
                    self._datos = DatosPlanta.desde_api(self._leer())
                    log.info("Datos de la planta: %d equipos, %d límites de alarma",
                             len(self._datos.catalogo.equipos), len(self._datos.limites))
                except Exception as e:
                    log.warning("No se pudo leer /api/planta: %s", e)
            return self._datos

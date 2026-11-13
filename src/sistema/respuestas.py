"""Lo que contesta el operador en la tablet, como habla en campo, pasado a algo que el sistema puede usar.

    "Sí, confirmo."                                              -> confirma
    "No. La uno estaba en ciento quince kilos, no en ciento doce." -> corrige el valor de B-101
    "Eso de mil seiscientos era de la dos, no de la uno."         -> corrige el equipo
    "Lo de la dos es en psi, no en kilos."                        -> corrige la unidad
    "Te faltó la tres, que estaba en ciento diez bar."            -> agrega una lectura
    "Lo del filtro B no lo dije."                                 -> saca una lectura
    "Era la dos." / "La correcta es la dos."                     -> elige un equipo
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .equipos import Catalogo
from .numeros import leer
from .texto import normalizar, oraciones

UNIDADES_RESPUESTA = {"gotas por minuto": "gotas/min", "kilos": "kg/cm2", "kilogramos": "kg/cm2", "psi": "psi",
                      "libras": "psi", "bar": "bar", "bares": "bar", "metros": "m", "grados": "degC"}
_UNIDAD_AL_FINAL = re.compile(r"^(?P<num>.*?)\s*(?P<uni>" + "|".join(UNIDADES_RESPUESTA) + r")?$")
_CONFIRMA = re.compile(r"\b(confirmo|correcto|asi es|esta bien|perfecto|dale)\b")
_NO_ENTIENDE = re.compile(r"\b(no te entendi|no te sigo|repetime|de que nota|confirmar que|mostrame)\b")

_VALOR = re.compile(r"^(?P<eq>.+?) estaba en (?P<num>.+?) no en (?P<viejo>.+)$")
_EQUIPO = re.compile(r"^eso de (?P<num>.+?) era de (?P<eq>.+?) no de (?P<otro>.+)$")
_UNIDAD = re.compile(r"^lo de (?P<eq>.+?) es en (?P<u1>.+?) no en (?P<u2>.+)$")
_FALTA = re.compile(r"^te falto (?P<eq>.+?) que estaba en (?P<num>.+)$")
_SOBRA = re.compile(r"^lo (?:de|del) (?P<eq>.+?) no lo dije$")


@dataclass
class Correccion:
    tipo: str                    # valor | equipo | unidad | falta | sobra
    tag: str | None
    valor: float | None = None
    anterior: float | None = None
    unidad: str | None = None
    tag_anterior: str | None = None


@dataclass
class Respuesta:
    tipo: str                    # confirma | corrige | elige | no_entiende | desconocida
    correcciones: list[Correccion] = field(default_factory=list)
    tag: str | None = None       # el equipo elegido, si eligió
    hay_mas: bool = False        # "y hay más cosas, revisalo todo"


def _numero_y_unidad(texto: str) -> tuple[float | None, str | None]:
    m = _UNIDAD_AL_FINAL.match(texto.strip())
    numero, unidad = (m.group("num"), m.group("uni")) if m else (texto, None)
    return leer(numero.split()), UNIDADES_RESPUESTA.get(unidad) if unidad else None


def _equipo(texto: str, catalogo: Catalogo) -> str | None:
    texto = texto.strip()
    if not texto.startswith(("el ", "la ", "al ", "del ")):
        texto = "el " + texto   # "lo del filtro B" -> "el filtro b"
    return catalogo.unico(texto) or catalogo.unico(texto[3:])


def interpretar(texto: str, catalogo: Catalogo) -> Respuesta:
    normal = normalizar(texto)
    if _NO_ENTIENDE.search(normal):
        return Respuesta("no_entiende")
    if not normal.startswith("no ") and normal != "no" and _CONFIRMA.search(normal):
        return Respuesta("confirma")
    if normal.startswith("no ") or normal == "no":
        respuesta = Respuesta("corrige")
        for oracion in oraciones(texto):
            o = normalizar(oracion)
            if o in ("no", ""):
                continue
            if "hay mas cosas" in o:
                respuesta.hay_mas = True
            elif m := _VALOR.match(o):
                valor, unidad = _numero_y_unidad(m["num"])
                anterior, _ = _numero_y_unidad(m["viejo"])
                respuesta.correcciones.append(Correccion("valor", _equipo(m["eq"], catalogo), valor, anterior, unidad))
            elif m := _EQUIPO.match(o):
                valor, _ = _numero_y_unidad(m["num"])
                respuesta.correcciones.append(Correccion("equipo", _equipo(m["eq"], catalogo), valor,
                                                         tag_anterior=_equipo(m["otro"], catalogo)))
            elif m := _UNIDAD.match(o):
                respuesta.correcciones.append(Correccion("unidad", _equipo(m["eq"], catalogo),
                                                         unidad=UNIDADES_RESPUESTA.get(m["u1"].strip())))
            elif m := _FALTA.match(o):
                valor, unidad = _numero_y_unidad(m["num"])
                respuesta.correcciones.append(Correccion("falta", _equipo(m["eq"], catalogo), valor, unidad=unidad))
            elif m := _SOBRA.match(o):
                respuesta.correcciones.append(Correccion("sobra", _equipo(m["eq"], catalogo)))
        return respuesta if respuesta.correcciones else Respuesta("desconocida")
    # Elegir entre opciones: "La dos.", "Era la dos.", "La correcta es la dos.", "Ninguna de esas, era la dos."
    menciones = catalogo.menciones(normal)
    if menciones:
        return Respuesta("elige", tag=menciones[-1].tag)
    return Respuesta("desconocida")

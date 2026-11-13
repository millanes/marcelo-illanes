"""Modelo de lenguaje local (Ollama) con LangChain: extracción del parte con structured output.

El modelo recibe el texto de la nota y devuelve filas con un esquema Pydantic estricto (extra="forbid").
Esa salida no se usa tal cual: pasa por las mismas normalizaciones que las filas de las reglas (el equipo
se resuelve contra /api/planta, la unidad sale del manómetro de cada bomba, "la bomba" a secas queda para
preguntar, un diferencial "dieciocho" se anota 1,8 y se confirma).

Control por nota (condiciones de corte):
- un solo llamado al modelo, sin reintentos ni herramientas;
- presupuesto de salida de MAX_TOKENS tokens (num_predict) y de contexto NUM_CTX;
- TIMEOUT_S de espera: si se pasa, falla o devuelve algo que no valida, el parte sigue con lo que
  entendieron las reglas.
Cada llamado queda registrado con el modelo, los tokens de entrada y salida y el tiempo.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .equipos import Catalogo
from .extractor import (AMBIGUA, DIFERENCIAL_ALTO, DIFERENCIAL_MAXIMO, RODAMIENTO_CALIENTE, TIPO_DE,
                        UNIDAD_FIJA, Fila)
from .texto import normalizar

log = logging.getLogger(__name__)

MAX_TOKENS = 400
NUM_CTX = 2048
TIMEOUT_S = 60.0

Variable = Literal["presion_descarga_local", "presion_diferencial", "nivel", "temperatura_rodamiento_ir",
                   "temperatura_motor_ir", "goteo_empaquetadura", "nivel_aceite_visor", "estado_correas", "ruido",
                   "estado"]


class FilaModelo(BaseModel):
    """Una lectura u observación de la nota, como la entiende el modelo."""
    model_config = ConfigDict(extra="forbid")
    equipo: str = Field(description="El equipo como lo nombró el operador, tal cual: 'la dos', 'la de la punta', "
                                    "'el filtro B', 'el de la izquierda', 'el pulmón'. Si dijo 'la bomba' sin "
                                    "decir cuál, poné 'la bomba'.")
    variable: Variable = Field(description="presion_descarga_local: 'está en X de descarga'. presion_diferencial: "
                                           "'me dio X de diferencial' (filtros). nivel: metros del pulmón. "
                                           "temperatura_rodamiento_ir: grados del rodamiento con la pistola. "
                                           "goteo_empaquetadura, nivel_aceite_visor, estado_correas, ruido y "
                                           "estado (parada, en reserva, disparada) son observaciones sin número.")
    valor: float | None = Field(description="El número dicho, en cifras: 'mil seiscientos veinte' es 1620, "
                                            "'uno coma ocho' y 'uno ocho' son 1.8, 'uno y medio' es 1.5. "
                                            "null si no dijo un número.")
    unidad: Literal["kg/cm2", "psi", "bar", "m", "degC"] | None = Field(
        description="La unidad si la dijo: kilos es kg/cm2, libras es psi, bar, metros es m, grados es degC. "
                    "null si no la dijo.")
    observacion: str | None = Field(description="Lo que dijo además del número, si es algo anormal o un estado: "
                                                 "'gotea por la empaquetadura', 'hace un golpeteo metálico', "
                                                 "'parada, en reserva'. null si no dijo nada de eso.")


class ParteModelo(BaseModel):
    """Todas las lecturas y observaciones de una nota de voz de la recorrida."""
    model_config = ConfigDict(extra="forbid")
    filas: list[FilaModelo] = Field(description="Una fila por equipo y variable, en el orden en que se dijeron.")


INSTRUCCIONES = (
    "Sos el asistente del operador de una planta de inyección de agua. Te paso la transcripción de una nota de "
    "voz de la recorrida (puede tener errores de transcripción, como 'medio' en lugar de 'me dio'). Devolvé "
    "las lecturas y observaciones que dice, una fila por equipo y variable. No inventes valores ni equipos: "
    "si algo no está en la nota, no lo pongas. Una nota que dice 'sin novedad' no tiene filas.")


@dataclass(frozen=True)
class Uso:
    """Un llamado al modelo: para el registro de tokens, costo y tiempo."""
    modelo: str
    tarea: str
    tokens_entrada: int
    tokens_salida: int
    segundos: float
    ok: bool
    error: str | None = None


_ANORMAL = ("rebals", "pierde", "perdida", "gotea", "goteo", "chorrea", "ruido", "golpe", "vibra", "calient",
            "alto", "bajo", "fuga", "humo", "flojas", "chillan", "piedras")


def _observacion(variable: str, texto: str | None) -> str | None:
    """La observación del modelo, llevada a los estados que usa el sistema. En una lectura con número solo
    se conserva si dice algo anormal: un modelo chico a veces repite "normal" o el valor, y eso escalaría."""
    if not texto:
        return None
    t = normalizar(texto)
    if variable == "estado":
        if "dispar" in t or "no arranca" in t:
            return "disparada, no arranca"
        if "mantenimiento" in t:
            return "parada por mantenimiento"
        if "parad" in t or "reserva" in t:
            return "parada, en reserva"
        return texto
    if variable in UNIDAD_FIJA or variable == "presion_descarga_local":
        return texto if any(p in t for p in _ANORMAL) else None
    return texto


def a_filas(filas: list[FilaModelo], catalogo: Catalogo) -> list[Fila]:
    """La salida del modelo, normalizada como las filas de las reglas."""
    salida: list[Fila] = []
    vistas: set[tuple[str | None, str]] = set()
    for m in filas:
        dicho = normalizar(m.equipo)
        tag = AMBIGUA if dicho in ("la bomba", "bomba") else catalogo.unico(dicho)
        if tag not in (None, AMBIGUA) and catalogo.equipos[tag].tipo != TIPO_DE.get(m.variable, catalogo.equipos[tag].tipo):
            tag = None
        if m.variable == "nivel" and tag is None:
            tag = next(iter(catalogo.de_tipo("tanque")), None)
        if (tag, m.variable) in vistas:
            continue
        fila = Fila(tag, m.variable, observacion=_observacion(m.variable, m.observacion))
        if m.valor is not None and m.variable in UNIDAD_FIJA.keys() | {"presion_descarga_local"}:
            fila.valor = m.valor
            fila.unidad = UNIDAD_FIJA.get(m.variable) or m.unidad or (
                catalogo.equipos[tag].unidad_manometro if tag not in (None, AMBIGUA) else None)
            if m.variable == "presion_diferencial" and fila.valor >= DIFERENCIAL_MAXIMO:
                fila.valor, fila.ambigua, fila.dicho = round(fila.valor / 10, 1), "valor", f"{m.valor:g}"
            if m.variable == "presion_diferencial" and fila.valor >= DIFERENCIAL_ALTO:
                fila.observacion = "diferencial alto"
            if m.variable == "temperatura_rodamiento_ir" and fila.valor > RODAMIENTO_CALIENTE:
                fila.observacion = "rodamiento caliente"
        elif fila.observacion is None:
            continue   # una variable de observación sin observación no dice nada
        if tag in (None, AMBIGUA):
            fila.tag, fila.ambigua, fila.dicho = None, "equipo", m.equipo
        salida.append(fila)
        vistas.add((tag, m.variable))
    return salida


class ExtractorLLM:
    """Extrae las filas del parte con un modelo local servido por Ollama."""

    def __init__(self, url: str, modelo: str, timeout_s: float = TIMEOUT_S, max_tokens: int = MAX_TOKENS,
                 cadena: Any = None, registrar: Callable[[Uso], None] | None = None) -> None:
        self.modelo = modelo
        if cadena is None:
            from langchain_ollama import ChatOllama
            chat = ChatOllama(model=modelo, base_url=url, temperature=0, num_predict=max_tokens, num_ctx=NUM_CTX,
                              keep_alive=-1, reasoning=False, client_kwargs={"timeout": timeout_s})
            cadena = chat.with_structured_output(ParteModelo, method="json_schema", include_raw=True)
        self._cadena = cadena
        self._registrar = registrar
        self._candado = threading.Lock()   # un llamado por vez: en CPU, dos a la vez tardan más que uno atrás del otro

    def extraer(self, texto: str) -> list[FilaModelo] | None:
        """Las filas que entiende el modelo, o None si no respondió algo válido dentro del presupuesto."""
        t0 = time.perf_counter()
        with self._candado:
            try:
                r = self._cadena.invoke([("system", INSTRUCCIONES), ("human", texto)])
                error = r.get("parsing_error")
                parte = r.get("parsed")
                uso = getattr(r.get("raw"), "usage_metadata", None) or {}
            except Exception as e:   # timeout, Ollama caído, respuesta rota
                parte, error, uso = None, e, {}
        self._anotar(Uso(self.modelo, "parte", uso.get("input_tokens", 0), uso.get("output_tokens", 0),
                         time.perf_counter() - t0, parte is not None, str(error)[:200] if error else None))
        if parte is None:
            log.warning("El modelo no devolvió un parte válido: %s", error)
            return None
        return parte.filas

    def _anotar(self, uso: Uso) -> None:
        log.info("Modelo %s (%s): %d tokens de entrada, %d de salida, %.1f s%s", uso.modelo, uso.tarea,
                 uso.tokens_entrada, uso.tokens_salida, uso.segundos, "" if uso.ok else f", falló: {uso.error}")
        if self._registrar:
            try:
                self._registrar(uso)
            except Exception:
                log.exception("No se pudo registrar el uso del modelo")

"""El latido: cada 30 s reales el sistema avisa que está vivo, si hay enlace con la base y cuántas cosas
tiene sin sincronizar.

El corrector cuenta como hueco cualquier intervalo de más de 60 s reales entre latidos, y en la corrección
el reloj de la planta arranca recién cuando llega el primero. Por eso el latido depende solo del broker:
no de la base ni de los modelos.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

from planta_contrato import EstadoSistema, Topicos
from pydantic import BaseModel

from .reloj import RelojPlanta

log = logging.getLogger(__name__)


class Publicador(Protocol):
    conectado: threading.Event

    def publicar(self, topico: str, mensaje: BaseModel) -> bool: ...


class Sonda(Protocol):
    def probar(self) -> bool: ...


def armar_latido(nombre: str, reloj: RelojPlanta, enlace_ok: bool, pendientes: int) -> EstadoSistema:
    return EstadoSistema(ts=reloj.ahora(), sistema=nombre, enlace_ok=enlace_ok, pendientes_sincronizar=pendientes)


def latir(bus: Publicador, nombre: str, reloj: RelojPlanta, sonda: Sonda, pendientes: Callable[[], int],
          intervalo_s: float, parar: threading.Event) -> None:
    """Publica el latido hasta que se pida parar. El primero sale apenas hay conexión con el broker."""
    proximo = 0.0
    primero = True
    while not parar.is_set():
        if not bus.conectado.is_set():
            parar.wait(1.0)
            continue
        espera = proximo - time.monotonic()
        if espera > 0:
            parar.wait(min(espera, 1.0))
            continue
        latido = armar_latido(nombre, reloj, sonda.probar(), pendientes())
        if not bus.publicar(Topicos.ESTADO, latido):
            parar.wait(1.0)   # se cortó el broker entre medio: se reintenta enseguida
            continue
        if primero:
            log.info("Primer latido publicado (enlace_ok=%s)", latido.enlace_ok)
            primero = False
        proximo = time.monotonic() + intervalo_s

"""Qué se hace con cada mensaje de la planta.

El hilo de red de MQTT no se puede trabar. Las lecturas de sensores, que llegan muchas veces por segundo,
se resuelven ahí mismo: se validan y se guarda la última de cada tag y variable. Notas, respuestas y
cierres van a una cola que atiende otro hilo: se validan contra el contrato, quedan registradas en
Postgres y se le pasan a los manejadores suscritos a ese tópico.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable

from planta_contrato import EstadoEnergia, LecturaSensor, NotaCierre, NotaRecorrida, RespuestaOperador, Topicos
from pydantic import BaseModel, ValidationError

from .persistencia import Almacen
from .reloj import RelojPlanta

log = logging.getLogger(__name__)

PREFIJO_SENSORES = "planta/sensores/"
MODELOS: dict[str, type[BaseModel]] = {
    Topicos.RECORRIDAS: NotaRecorrida,
    Topicos.RESPUESTAS: RespuestaOperador,
    Topicos.CIERRES: NotaCierre,
    Topicos.ENERGIA: EstadoEnergia,
    # planta/planillas (la planilla de papel del turno) no tiene modelo en el contrato: se guarda tal cual.
}

Manejador = Callable[[BaseModel], None]


class Despachador:
    def __init__(self, reloj: RelojPlanta, almacen: Almacen | None = None) -> None:
        self.reloj = reloj
        self.almacen = almacen
        self.lecturas: dict[tuple[str, str], LecturaSensor] = {}   # la última de cada tag y variable
        self.hay_energia: bool | None = None
        self._manejadores: dict[str, list[Manejador]] = {}
        self._cola: queue.Queue[tuple[str, bytes] | None] = queue.Queue()
        self._hilo = threading.Thread(target=self._atender, name="despacho", daemon=True)

    def suscribir(self, topico: str, manejador: Manejador) -> None:
        self._manejadores.setdefault(topico, []).append(manejador)

    def arrancar(self) -> None:
        self._hilo.start()

    def detener(self) -> None:
        self._cola.put(None)

    @property
    def en_cola(self) -> int:
        return self._cola.qsize()

    def recibir(self, topico: str, payload: bytes) -> None:
        """Lo llama el hilo de red de MQTT por cada mensaje."""
        if topico.startswith(PREFIJO_SENSORES):
            self._lectura(payload)
        else:
            self._cola.put((topico, payload))

    def _lectura(self, payload: bytes) -> None:
        try:
            lectura = LecturaSensor.model_validate_json(payload)
        except ValidationError as e:
            log.warning("Lectura de sensor inválida: %s", e.errors()[0]["msg"])
            return
        if not self.lecturas:
            log.info("Llegan lecturas de sensores; hora de la planta: %s", lectura.ts.isoformat())
        self.lecturas[(lectura.tag, lectura.variable)] = lectura
        self.reloj.observar(lectura.ts)

    def _atender(self) -> None:
        if self.almacen:
            self.almacen.preparar()
        while (item := self._cola.get()) is not None:
            self.procesar(*item)

    def procesar(self, topico: str, payload: bytes) -> None:
        try:
            datos = json.loads(payload)
            modelo = MODELOS.get(topico)
            mensaje = modelo.model_validate(datos) if modelo else None
        except ValueError as e:   # JSON roto o fuera del contrato (ValidationError también es ValueError)
            log.warning("Mensaje inválido en %s: %s", topico, e)
            return
        if self.almacen:
            self.almacen.guardar("entrada", topico, datos)
        if mensaje is None:
            log.info("Recibido %s", topico)
            return
        if isinstance(mensaje, EstadoEnergia):
            self.hay_energia = mensaje.hay_energia
        log.info("Recibido %s: %s", topico, _resumen(mensaje))
        for manejador in self._manejadores.get(topico, []):
            try:
                manejador(mensaje)
            except Exception:  # un manejador roto no frena a los demás ni al hilo
                log.exception("Falló un manejador de %s", topico)


def _resumen(m: BaseModel) -> str:
    if isinstance(m, NotaRecorrida):
        return f"nota {m.id} de {m.operador}, {len(m.fotos)} foto(s)"
    if isinstance(m, RespuestaOperador):
        return f"respuesta a {m.mensaje_id}: {m.texto!r}"
    if isinstance(m, NotaCierre):
        return f"cierre de {m.orden_id} por {m.tecnico}"
    if isinstance(m, EstadoEnergia):
        return "hay energía" if m.hay_energia else "SIN ENERGÍA"
    return type(m).__name__

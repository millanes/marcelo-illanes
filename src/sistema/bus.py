"""El broker MQTT de la planta.

Se suscribe a lo que publica la planta y publica mensajes del contrato. Si se corta la conexión, paho
reconecta solo y al reconectar se vuelve a suscribir.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

import paho.mqtt.client as mqtt
from planta_contrato import Topicos
from pydantic import BaseModel

log = logging.getLogger(__name__)

# Los sensores llegan muchas veces por segundo y la planta los publica con QoS 0; el resto, con QoS 1.
SUSCRIPCIONES: list[tuple[str, int]] = [
    ("planta/sensores/#", 0),
    (Topicos.RECORRIDAS, 1),
    (Topicos.RESPUESTAS, 1),
    (Topicos.ENERGIA, 1),
    (Topicos.CIERRES, 1),
    ("planta/planillas", 1),
]

Receptor = Callable[[str, bytes], None]


class Bus:
    def __init__(self, host: str, port: int, nombre: str, receptor: Receptor) -> None:
        self.host, self.port = host, port
        self._receptor = receptor
        # Sesión limpia: al arrancar no llegan mensajes viejos que el broker hubiera guardado para este cliente.
        self._cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{nombre}-{uuid.uuid4().hex[:6]}")
        self._cliente.on_connect = self._al_conectar
        self._cliente.on_disconnect = self._al_desconectar
        self._cliente.on_message = self._al_recibir
        self._cliente.reconnect_delay_set(min_delay=1, max_delay=10)
        self.conectado = threading.Event()

    def arrancar(self) -> None:
        self._cliente.connect_async(self.host, self.port, keepalive=30)
        self._cliente.loop_start()

    def detener(self) -> None:
        self._cliente.disconnect()
        self._cliente.loop_stop()

    def publicar(self, topico: str, mensaje: BaseModel) -> bool:
        """Publica un modelo del contrato. Validarlo es construirlo: si llegó hasta acá, cumple el contrato."""
        if not self.conectado.is_set():
            log.warning("Sin conexión con el broker: no se publicó en %s", topico)
            return False
        info = self._cliente.publish(topico, mensaje.model_dump_json(), qos=1)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def _al_conectar(self, cliente: mqtt.Client, _userdata, _flags, codigo, _props) -> None:
        if codigo.is_failure:
            log.warning("El broker rechazó la conexión: %s", codigo)
            return
        cliente.subscribe(SUSCRIPCIONES)
        self.conectado.set()
        log.info("Conectado al broker %s:%s", self.host, self.port)

    def _al_desconectar(self, _cliente, _userdata, _flags, codigo, _props) -> None:
        self.conectado.clear()
        log.warning("Se cortó la conexión con el broker (%s); reintentando", codigo)

    def _al_recibir(self, _cliente, _userdata, msg: mqtt.MQTTMessage) -> None:
        try:
            self._receptor(msg.topic, msg.payload)
        except Exception:  # un mensaje que falla no puede tirar abajo el hilo de red
            log.exception("Error al recibir %s", msg.topic)

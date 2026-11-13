"""Arranque: conecta con el broker y con Postgres, atiende los mensajes de la planta y late cada 30 s.

    python -m sistema
"""

from __future__ import annotations

import logging
import signal
import threading

from planta_contrato import Topicos

from .bus import Bus
from .config import Config
from .despacho import Despachador
from .dialogo import Dialogo
from .enlace import SondaEnlace
from .equipos import FuenteDatosPlanta
from .historian import Historian
from .latido import latir
from .notas import ProcesadorNotas
from .persistencia import Almacen
from .planta import ClientePlanta
from .reloj import RelojPlanta
from .transcripcion import TranscriptorWhisper

log = logging.getLogger("sistema")


def sin_pendientes() -> int:
    """Todavía no hay nada que sincronizar con la base: la cola de salida llega con las órdenes."""
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for ruidoso in ("httpx", "faster_whisper"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
    cfg = Config.desde_entorno()

    parar = threading.Event()
    for senal in (signal.SIGTERM, signal.SIGINT):
        signal.signal(senal, lambda *_: parar.set())

    reloj = RelojPlanta()
    almacen = Almacen(cfg.database_url)
    despachador = Despachador(reloj, almacen)
    bus = Bus(cfg.mqtt_host, cfg.mqtt_port, cfg.nombre_sistema, despachador.recibir)
    sonda = SondaEnlace(cfg.enlace_url, cfg.enlace_timeout_s)
    planta = ClientePlanta(cfg.sim_url)
    fuente = FuenteDatosPlanta(planta.planta)
    historian = Historian(planta)
    notas = ProcesadorNotas(planta, lambda: TranscriptorWhisper(cfg.stt_modelo, cfg.stt_hilos, cfg.stt_beam),
                            bus, reloj, almacen, fuente)
    dialogo = Dialogo(bus, reloj, fuente, historian, almacen)
    notas.siguientes.append(dialogo.nueva_nota)
    despachador.suscribir(Topicos.RECORRIDAS, notas.recibir)
    despachador.suscribir(Topicos.RESPUESTAS, dialogo.recibir_respuesta)

    def precargar_rangos() -> None:
        """Los rangos de 45 días se leen con la hora de la planta: hay que esperar la primera lectura."""
        while not reloj.sincronizado and not parar.wait(1.0):
            pass
        datos = fuente.obtener()
        if datos and not parar.is_set():
            historian.precargar([(t, "presion_descarga") for t in datos.catalogo.de_tipo("bomba")]
                                + [(t, "presion_diferencial") for t in datos.catalogo.de_tipo("filtro")], reloj.ahora())

    log.info("Arranca %s: broker %s:%s, planta %s", cfg.nombre_sistema, cfg.mqtt_host, cfg.mqtt_port, cfg.sim_url)
    despachador.arrancar()
    notas.arrancar()     # carga los datos de la planta y el modelo en su hilo: el latido no los espera
    dialogo.arrancar()   # retoma las conversaciones abiertas que quedaron en Postgres
    bus.arrancar()
    threading.Thread(target=precargar_rangos, name="rangos", daemon=True).start()
    try:
        latir(bus, cfg.nombre_sistema, reloj, sonda, sin_pendientes, cfg.latido_s, parar)
    finally:
        bus.detener()
        notas.detener()
        dialogo.detener()
        despachador.detener()
        log.info("Sistema detenido")


if __name__ == "__main__":
    main()

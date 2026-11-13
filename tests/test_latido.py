import threading
import time
from datetime import datetime, timedelta, timezone

from planta_contrato import EstadoSistema, Topicos

from sistema.latido import armar_latido, latir
from sistema.reloj import RelojPlanta

HORA_PLANTA = datetime(2026, 11, 16, 15, 21, 20, tzinfo=timezone(timedelta(hours=-3)))


class BusFalso:
    def __init__(self, conectado: bool = True) -> None:
        self.conectado = threading.Event()
        if conectado:
            self.conectado.set()
        self.publicados: list = []

    def publicar(self, topico, mensaje) -> bool:
        self.publicados.append((topico, mensaje))
        return True


class SondaFija:
    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def probar(self) -> bool:
        return self.ok


def test_el_latido_cumple_el_contrato():
    reloj = RelojPlanta()
    reloj.observar(HORA_PLANTA)
    latido = armar_latido("marcelo-illanes", reloj, enlace_ok=True, pendientes=0)
    assert EstadoSistema.model_validate_json(latido.model_dump_json()) == latido


def test_el_latido_lleva_la_hora_de_la_planta_y_no_la_de_la_maquina():
    reloj = RelojPlanta()
    reloj.observar(HORA_PLANTA)
    assert armar_latido("x", reloj, enlace_ok=False, pendientes=2).ts == HORA_PLANTA


def correr_latido(bus, sonda, segundos: float):
    parar = threading.Event()
    hilo = threading.Thread(target=latir, args=(bus, "x", RelojPlanta(), sonda, lambda: 3, 0.05, parar))
    hilo.start()
    time.sleep(segundos)
    parar.set()
    hilo.join(timeout=3)
    return hilo


def test_late_seguido_y_se_detiene_cuando_se_le_pide():
    bus = BusFalso()
    hilo = correr_latido(bus, SondaFija(False), 0.3)
    assert not hilo.is_alive()
    assert len(bus.publicados) >= 3
    topico, latido = bus.publicados[0]
    assert topico == Topicos.ESTADO
    assert latido.enlace_ok is False and latido.pendientes_sincronizar == 3


def test_sin_broker_no_intenta_publicar():
    bus = BusFalso(conectado=False)
    correr_latido(bus, SondaFija(True), 0.2)
    assert bus.publicados == []

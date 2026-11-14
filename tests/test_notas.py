import threading
from datetime import datetime, timedelta, timezone

import pytest
from conftest import PLANTA, wav
from planta_contrato import EventoSistema, MensajeTablet, NotaRecorrida, Topicos

from sistema.notas import ProcesadorNotas
from sistema.reloj import RelojPlanta

S1 = ("¡Atención! Se rompió algo en la descarga de la uno, sale agua a presión para todos lados, me alejé. "
      "Saqué foto desde lejos.")
NORMAL = "La del medio está en mil seiscientos treinta de descarga, hace un golpeteo metálico."


class PlantaFalsa:
    def __init__(self, con_catalogo: bool = True) -> None:
        self.con_catalogo = con_catalogo

    def planta(self) -> dict:
        if not self.con_catalogo:
            raise ConnectionError("la planta no contesta")
        return PLANTA

    def descargar(self, url: str) -> bytes:
        return wav(8.0)


class TranscriptorFijo:
    def __init__(self, texto: str) -> None:
        self.texto = texto

    def transcribir(self, audio: bytes) -> str:
        return self.texto


class BusFalso:
    def __init__(self) -> None:
        self.conectado = threading.Event()
        self.conectado.set()
        self.publicados: list = []

    def publicar(self, topico, mensaje) -> bool:
        self.publicados.append((topico, mensaje))
        return True


def nota(audio: str | None = "/media/audios/NOTA-00200.wav") -> NotaRecorrida:
    return NotaRecorrida(id="NOTA-00200", ts=datetime(2026, 11, 18, 8, 40, tzinfo=timezone(timedelta(hours=-3))),
                         operador="Operador de prueba", audio_url=audio, fotos=["/media/fotos/rotura_descarga/01.jpg"],
                         recorrida_id="REC-00201")


def procesador(texto: str, bus: BusFalso, planta=None) -> ProcesadorNotas:
    return ProcesadorNotas(planta or PlantaFalsa(), lambda: TranscriptorFijo(texto), bus, RelojPlanta())


def test_una_condicion_insegura_sale_en_la_tablet_y_como_evento():
    bus = BusFalso()
    resultado = procesador(S1, bus).procesar(nota())
    assert [h.codigo for h in resultado.hallazgos] == ["S1"]
    (t_aviso, aviso), (t_evento, evento) = bus.publicados
    assert (t_aviso, t_evento) == (Topicos.TABLET, Topicos.EVENTOS)

    # Validan contra el contrato tal como viajan por MQTT.
    aviso = MensajeTablet.model_validate_json(aviso.model_dump_json())
    evento = EventoSistema.model_validate_json(evento.model_dump_json())
    assert aviso.nota_ids == ["NOTA-00200"] and aviso.espera_respuesta is False
    assert "procedimiento de emergencia" in aviso.texto and "radio" in aviso.texto
    assert evento.tipo.value == "AlertaSeguridad" and evento.origen == "parte"
    assert evento.nota_ids == ["NOTA-00200"] and evento.equipo == "B-101"


def test_los_ids_son_idempotentes():
    bus = BusFalso()
    p = procesador(S1, bus)
    p.procesar(nota())
    p.procesar(nota())
    assert {m.id for _, m in bus.publicados} == {"TAB-NOTA-00200-SEG", "EV-NOTA-00200-SEG"}


def test_una_nota_normal_no_publica_alertas():
    bus = BusFalso()
    resultado = procesador(NORMAL, bus).procesar(nota())
    assert resultado.hallazgos == [] and bus.publicados == []
    assert resultado.texto == NORMAL and resultado.segundos_audio == pytest.approx(8.0)


def test_sin_catalogo_la_alerta_sale_igual_sin_equipo():
    bus = BusFalso()
    procesador(S1, bus, PlantaFalsa(con_catalogo=False)).procesar(nota())
    evento = bus.publicados[1][1]
    assert evento.tipo.value == "AlertaSeguridad" and evento.equipo is None


def test_una_nota_sin_audio_se_saltea():
    bus = BusFalso()
    assert procesador(S1, bus).procesar(nota(audio=None)) is None
    assert bus.publicados == []


class AlmacenFalso:
    def __init__(self):
        self.fotos, self.notas = [], []

    def guardar_foto(self, nota_id, url, contenido):
        self.fotos.append((nota_id, url, len(contenido)))
        return True

    def guardar_nota(self, *args):
        self.notas.append(args)
        return True

    def guardar(self, *args):
        return True


def test_baja_las_fotos_de_la_nota_y_las_guarda():
    almacen = AlmacenFalso()
    p = ProcesadorNotas(PlantaFalsa(), lambda: TranscriptorFijo(NORMAL), BusFalso(), RelojPlanta(), almacen)
    resultado = p.procesar(nota())
    assert resultado.fotos == 1
    assert [(n, u) for n, u, _ in almacen.fotos] == [("NOTA-00200", "/media/fotos/rotura_descarga/01.jpg")]


def test_una_foto_que_no_baja_no_frena_la_nota():
    class PlantaSinFotos(PlantaFalsa):
        def descargar(self, url):
            if "/fotos/" in url:
                raise ConnectionError("no hay foto")
            return super().descargar(url)

    p = ProcesadorNotas(PlantaSinFotos(), lambda: TranscriptorFijo(NORMAL), BusFalso(), RelojPlanta(), AlmacenFalso())
    resultado = p.procesar(nota())
    assert resultado.fotos == 0 and resultado.texto == NORMAL


def test_le_pasa_la_nota_transcripta_al_paso_siguiente():
    recibidas = []
    p = procesador(NORMAL, BusFalso())
    p.siguientes.append(recibidas.append)
    p.procesar(nota())
    assert [r.texto for r in recibidas] == [NORMAL]

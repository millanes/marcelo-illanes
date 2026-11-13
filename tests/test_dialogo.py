import threading
from datetime import datetime, timedelta, timezone

import pytest
from conftest import PLANTA
from planta_contrato import EventoSistema, MensajeTablet, NotaRecorrida, RespuestaOperador, Topicos

from sistema import dialogo as modulo
from sistema.dialogo import Dialogo
from sistema.equipos import FuenteDatosPlanta
from sistema.notas import NotaProcesada
from sistema.reloj import RelojPlanta
from sistema.seguridad import detectar

TS = datetime(2026, 11, 18, 8, 40, tzinfo=timezone(timedelta(hours=-3)))


class BusFalso:
    def __init__(self) -> None:
        self.conectado = threading.Event()
        self.conectado.set()
        self.publicados: list = []

    def publicar(self, topico, mensaje) -> bool:
        # Lo que se publica tiene que cumplir el contrato tal como viaja por MQTT.
        modelo = MensajeTablet if topico == Topicos.TABLET else EventoSistema
        self.publicados.append((topico, modelo.model_validate_json(mensaje.model_dump_json())))
        return True

    def tablet(self) -> list[MensajeTablet]:
        return [m for t, m in self.publicados if t == Topicos.TABLET]

    def eventos(self) -> set[tuple]:
        return {(m.equipo, m.tipo.value) for t, m in self.publicados if t == Topicos.EVENTOS}


class HistorianFalso:
    def rango(self, tag, variable, hasta):
        return (100.0, 120.0) if variable == "presion_descarga" else (0.3, 1.6)

    def tendencia(self, tag, variable, hasta, horas=6):
        return "vibración últimas 6 h: 2.1 a 2.6 (prom. 2.3), estable"


@pytest.fixture
def bus():
    return BusFalso()


@pytest.fixture
def dialogo(bus):
    return Dialogo(bus, RelojPlanta(), FuenteDatosPlanta(lambda: PLANTA), HistorianFalso())


def nota(texto: str, nota_id: str = "NOTA-00400", fotos=()) -> NotaProcesada:
    n = NotaRecorrida(id=nota_id, ts=TS, operador="Operador de prueba", audio_url=f"/media/audios/{nota_id}.wav",
                      fotos=list(fotos), recorrida_id="REC-00401")
    return NotaProcesada(n, texto, detectar(texto), 8.0)


def contesta(dialogo: Dialogo, mensaje: MensajeTablet, texto: str) -> None:
    dialogo.responder(RespuestaOperador(id=f"RESP-{mensaje.id}", ts=TS, mensaje_id=mensaje.id,
                                        operador="Operador de prueba", texto=texto, audio_url=None))


def numericas(mensaje: MensajeTablet) -> list[tuple]:
    return [(f.tag, f.variable, f.valor, f.unidad) for f in mensaje.pantalla.filas if f.valor is not None]


def test_borrador_confirmado_al_primer_intento(dialogo, bus):
    dialogo.iniciar(nota("La uno está en ciento catorce kilos de descarga. El filtro B me dio cero nueve de diferencial."))
    [borrador] = bus.tablet()
    assert borrador.espera_respuesta and not borrador.pantalla.opciones   # no es una pregunta
    assert numericas(borrador) == [("B-101", "presion_descarga_local", 114, "kg/cm2"),
                                   ("F-201B", "presion_diferencial", 0.9, "kg/cm2")]
    contesta(dialogo, borrador, "Sí, confirmo.")
    assert bus.tablet()[-1].espera_respuesta is False
    assert bus.eventos() == {("B-101", "SinNovedad"), ("F-201B", "SinNovedad")}
    assert dialogo.conversaciones["NOTA-00400"].estado == "confirmado"


def test_aplica_las_correcciones_y_manda_otro_borrador(dialogo, bus):
    dialogo.iniciar(nota("La uno está en ciento doce kilos de descarga, gotea por la empaquetadura, le saqué foto.",
                         fotos=["/media/fotos/empaquetadura/03.jpg"]))
    contesta(dialogo, bus.tablet()[0], "No. La uno estaba en ciento quince kilos, no en ciento doce. Te faltó la uno, "
                                       "que estaba en cuarenta y cinco gotas por minuto. Corregilo y te confirmo.")
    segundo = bus.tablet()[-1]
    assert segundo.id.endswith("-B2")
    assert numericas(segundo) == [("B-101", "presion_descarga_local", 115, "kg/cm2"),
                                  ("B-101", "goteo_empaquetadura", 45, "gotas/min")]
    contesta(dialogo, segundo, "Correcto, dale.")
    assert bus.eventos() == {("B-101", "Escalado")}
    escalado = [m for t, m in bus.publicados if t == Topicos.EVENTOS][0]
    assert "/media/fotos/empaquetadura/03.jpg" in escalado.resumen and "vibración" in escalado.resumen.lower()


def test_una_correccion_con_otra_unidad_va_a_la_lectura_que_corresponde(dialogo, bus):
    # El borrador solo tenía la temperatura; el operador marca la descarga como "valor" sobre esa fila.
    dialogo.iniciar(nota("La del medio, el rodamiento me dio cincuenta y un grados con la pistola."))
    contesta(dialogo, bus.tablet()[0], "No. La dos estaba en mil seiscientos diez psi, no en cincuenta y uno. Te faltó "
                                       "la dos, que estaba en cincuenta y uno grados. Arreglá eso.")
    assert sorted(numericas(bus.tablet()[-1])) == [("B-102", "presion_descarga_local", 1610, "psi"),
                                                   ("B-102", "temperatura_rodamiento_ir", 51, "degC")]


def test_si_la_correccion_no_cambia_nada_no_repite_el_borrador(dialogo, bus):
    dialogo.iniciar(nota("La uno está en ciento catorce kilos de descarga."))
    contesta(dialogo, bus.tablet()[0], "No. Lo de el caudalímetro no lo dije. Con eso está.")
    assert len(bus.tablet()) == 1
    assert dialogo.conversaciones["NOTA-00400"].estado == "sin_confirmar"


def test_la_bomba_a_secas_pregunta_cual_antes_del_borrador(dialogo, bus):
    dialogo.iniciar(nota("Listo, la bomba está en mil seiscientos veinte psi de descarga."))
    [pregunta] = bus.tablet()
    assert pregunta.espera_respuesta and not pregunta.pantalla.filas
    assert pregunta.pantalla.opciones[0] == "La dos (B-102)"   # la de manómetro en psi va primero
    contesta(dialogo, pregunta, "Era la dos.")
    borrador = bus.tablet()[-1]
    assert numericas(borrador) == [("B-102", "presion_descarga_local", 1620, "psi")]


def test_un_diferencial_dicho_dieciocho_se_anota_uno_ocho_y_se_pregunta(dialogo, bus):
    dialogo.iniciar(nota("El filtro B me dio dieciocho de diferencial."))
    [borrador] = bus.tablet()
    assert numericas(borrador) == [("F-201B", "presion_diferencial", 1.8, "kg/cm2")]
    assert borrador.pantalla.opciones   # con opciones cuenta como pregunta
    contesta(dialogo, borrador, "Sí, confirmo.")
    assert bus.eventos() == {("F-201B", "Escalado")}   # 1,8 está sobre la alarma de 1,5


def test_sin_lecturas_con_numero_se_informa_sin_preguntar(dialogo, bus):
    dialogo.iniciar(nota("La tres está parada, en reserva."))
    [aviso] = bus.tablet()
    assert aviso.espera_respuesta is False and aviso.pantalla.filas
    assert bus.eventos() == {("B-103", "SinNovedad")}


def test_recorrida_sin_novedad(dialogo, bus):
    dialogo.iniciar(nota("Recorrida sin novedad, todo normal."))
    assert bus.tablet()[0].pantalla.filas and bus.eventos() == {(None, "SinNovedad")}


def test_condicion_insegura_queda_en_el_parte_sin_eventos_de_deteccion(dialogo, bus):
    dialogo.iniciar(nota("¡Atención! Se rompió algo en la descarga de la uno, sale agua a presión para todos lados.",
                         fotos=["/media/fotos/rotura_descarga/02.jpg"]))
    [aviso] = bus.tablet()
    assert not aviso.espera_respuesta
    assert [(f.tag, f.variable) for f in aviso.pantalla.filas] == [("B-101", "condicion_insegura")]
    assert bus.eventos() == set()


def test_avisa_un_valor_fuera_del_rango_historico(dialogo, bus):
    dialogo.iniciar(nota("La uno está en ciento sesenta kilos de descarga."))
    assert "fuera de lo que marcó" in bus.tablet()[0].texto


def test_sin_respuesta_se_cierra_con_lo_que_hay(dialogo, bus, monkeypatch):
    dialogo.iniciar(nota("La uno está en ciento catorce kilos de descarga."))
    monkeypatch.setattr(modulo, "ESPERA_MAX_S", -1)
    dialogo._vencidas()
    assert dialogo.conversaciones["NOTA-00400"].estado == "sin_respuesta"
    assert bus.eventos() == {("B-101", "SinNovedad")}


def test_una_respuesta_a_otro_mensaje_no_se_mezcla(dialogo, bus):
    dialogo.iniciar(nota("La uno está en ciento catorce kilos de descarga."))
    dialogo.responder(RespuestaOperador(id="R-1", ts=TS, mensaje_id="TAB-OTRA-B1", operador="x", texto="Sí, confirmo.",
                                        audio_url=None))
    assert dialogo.conversaciones["NOTA-00400"].estado == "borrador"

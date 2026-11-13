import threading
from datetime import datetime, timedelta, timezone

import pytest
from conftest import PLANTA
from planta_contrato import MensajeTablet, NotaRecorrida, Topicos
from pydantic import ValidationError

from sistema.dialogo import Dialogo
from sistema.equipos import Catalogo, FuenteDatosPlanta
from sistema.llm import ExtractorLLM, FilaModelo, ParteModelo, a_filas
from sistema.notas import NotaProcesada
from sistema.reloj import RelojPlanta


@pytest.fixture
def catalogo(datos_planta) -> Catalogo:
    return Catalogo.desde_api(datos_planta)


def fila(equipo, variable, valor=None, unidad=None, observacion=None) -> FilaModelo:
    return FilaModelo(equipo=equipo, variable=variable, valor=valor, unidad=unidad, observacion=observacion)


class Uso:
    input_tokens = 0


class Respuesta:
    def __init__(self, tokens_entrada=230, tokens_salida=60):
        self.usage_metadata = {"input_tokens": tokens_entrada, "output_tokens": tokens_salida}


class CadenaFalsa:
    """Lo que devuelve `with_structured_output(..., include_raw=True)`: raw, parsed y parsing_error."""

    def __init__(self, parte=None, error=None, excepcion=None):
        self.parte, self.error, self.excepcion, self.llamados = parte, error, excepcion, 0

    def invoke(self, mensajes):
        self.llamados += 1
        if self.excepcion:
            raise self.excepcion
        return {"raw": Respuesta(), "parsed": self.parte, "parsing_error": self.error}


def test_el_esquema_es_estricto():
    with pytest.raises(ValidationError):
        FilaModelo(equipo="la dos", variable="presion_descarga_local", valor=1620, unidad="psi", observacion=None,
                   comentario="de más")
    with pytest.raises(ValidationError):
        FilaModelo(equipo="la dos", variable="presion_de_algo", valor=1, unidad=None, observacion=None)


def test_la_salida_del_modelo_se_normaliza_como_las_reglas(catalogo):
    filas = a_filas([fila("la de la punta", "presion_descarga_local", 114),
                     fila("el filtro B", "presion_diferencial", 18),
                     fila("la bomba", "estado", observacion="parada en reserva"),
                     fila("el pulmón", "nivel", 5.5, "m", observacion="normal")], catalogo)
    assert [(f.tag, f.variable, f.valor, f.unidad, f.observacion, f.ambigua) for f in filas] == [
        ("B-101", "presion_descarga_local", 114, "kg/cm2", None, None),       # unidad del manómetro de la B-101
        ("F-201B", "presion_diferencial", 1.8, "kg/cm2", "diferencial alto", "valor"),   # "dieciocho"
        (None, "estado", None, None, "parada, en reserva", "equipo"),          # "la bomba" a secas: preguntar
        ("TK-301", "nivel", 5.5, "m", None, None),                             # "normal" no es una observación
    ]


def test_registra_tokens_y_tiempo():
    usos = []
    cadena = CadenaFalsa(ParteModelo(filas=[fila("la dos", "presion_descarga_local", 1620, "psi")]))
    extractor = ExtractorLLM("http://ollama:11434", "modelo-de-prueba", cadena=cadena, registrar=usos.append)
    assert extractor.extraer("La dos está en 1620 psi de descarga.")[0].valor == 1620
    [uso] = usos
    assert (uso.modelo, uso.tokens_entrada, uso.tokens_salida, uso.ok) == ("modelo-de-prueba", 230, 60, True)


@pytest.mark.parametrize("cadena", [CadenaFalsa(error=ValueError("no es JSON")),
                                    CadenaFalsa(excepcion=TimeoutError("se pasó del tiempo"))])
def test_si_falla_o_no_valida_devuelve_none_y_lo_registra(cadena):
    usos = []
    assert ExtractorLLM("u", "m", cadena=cadena, registrar=usos.append).extraer("texto") is None
    assert usos[0].ok is False and usos[0].error


# ------------------------------------------------------------------ cuándo entra el modelo en el diálogo

class BusFalso:
    def __init__(self):
        self.conectado = threading.Event()
        self.conectado.set()
        self.publicados = []

    def publicar(self, topico, mensaje):
        self.publicados.append((topico, mensaje))
        return True


def procesada(texto):
    nota = NotaRecorrida(id="NOTA-00500", ts=datetime(2026, 11, 18, 8, tzinfo=timezone(timedelta(hours=-3))),
                         operador="x", audio_url="/media/audios/NOTA-00500.wav", fotos=[], recorrida_id="REC-1")
    return NotaProcesada(nota, texto, [], 6.0)


def dialogo(cadena, modo="mixto"):
    bus = BusFalso()
    llm = ExtractorLLM("u", "m", cadena=cadena)
    return Dialogo(bus, RelojPlanta(), FuenteDatosPlanta(lambda: PLANTA), llm=llm, modo=modo), bus


def test_en_modo_mixto_el_modelo_entra_solo_si_las_reglas_no_entienden():
    cadena = CadenaFalsa(ParteModelo(filas=[fila("la tres", "presion_descarga_local", 110, "bar")]))
    d, bus = dialogo(cadena)
    d.iniciar(procesada("La uno está en ciento catorce kilos de descarga."))   # las reglas alcanzan
    assert cadena.llamados == 0
    d.iniciar(procesada("Che, la tres anda con ciento diez de presión en la salida."))   # las reglas no
    assert cadena.llamados == 1
    borrador = [m for t, m in bus.publicados if t == Topicos.TABLET][-1]
    assert isinstance(borrador, MensajeTablet)
    assert [(f.tag, f.valor, f.unidad) for f in borrador.pantalla.filas] == [("B-103", 110, "bar")]


def test_si_el_modelo_falla_sigue_con_las_reglas():
    d, bus = dialogo(CadenaFalsa(excepcion=ConnectionError("ollama caído")), modo="llm")
    d.iniciar(procesada("La uno está en ciento catorce kilos de descarga."))
    borrador = [m for t, m in bus.publicados if t == Topicos.TABLET][-1]
    assert [(f.tag, f.valor) for f in borrador.pantalla.filas] == [("B-101", 114)]

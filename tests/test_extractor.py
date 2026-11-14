import pytest

from sistema.equipos import Catalogo
from sistema.extractor import extraer


@pytest.fixture
def catalogo(datos_planta) -> Catalogo:
    return Catalogo.desde_api(datos_planta)


def filas(texto, catalogo, fotos=None):
    return [(f.tag, f.variable, f.valor, f.unidad, f.observacion, f.ambigua) for f in extraer(texto, catalogo, fotos)]


# Notas como las dicta el operador de la planta.
@pytest.mark.parametrize("texto, esperado", [
    ("La dos está en mil seiscientos veinte de descarga, ah, y el filtro B me dio uno ocho de diferencial.",
     [("B-102", "presion_descarga_local", 1620, "psi", None, None),
      ("F-201B", "presion_diferencial", 1.8, "kg/cm2", "diferencial alto", None)]),
    ("La de la punta está en ciento catorce kilos de descarga, el rodamiento me dio setenta y ocho grados con la pistola.",
     [("B-101", "presion_descarga_local", 114, "kg/cm2", None, None),
      ("B-101", "temperatura_rodamiento_ir", 78, "degC", "rodamiento caliente", None)]),
    ("El pulmón está en tres coma dos metros, está rebalsando a la pileta.",
     [("TK-301", "nivel", 3.2, "m", "rebalse a pileta", None)]),
    ("La tres está parada, en reserva.", [("B-103", "estado", None, None, "parada, en reserva", None)]),
    ("La tres está disparada, no arranca.", [("B-103", "estado", None, None, "disparada, no arranca", None)]),
    ("La uno chorrea por la empaquetadura, le saqué foto.",
     [("B-101", "goteo_empaquetadura", None, None, "pérdida importante por empaquetadura", None)]),
    ("En la dos, las correas están flojas, chillan, saqué foto.",
     [("B-102", "estado_correas", None, None, "correas flojas", None)]),
    ("La del medio está en mil seiscientos treinta de descarga, hace un golpeteo metálico.",
     [("B-102", "presion_descarga_local", 1630, "psi", None, None),
      ("B-102", "ruido", None, None, "hace un golpeteo metálico", None)]),
    ("Recorrida sin novedad, todo normal.", []),
])
def test_notas_del_operador(catalogo, texto, esperado):
    assert filas(texto, catalogo) == esperado


# Transcripciones reales de Whisper en la corrección, con sus errores.
@pytest.mark.parametrize("texto, esperado", [
    ("El de la izquierda me dio uno con cuatro de diferencial. La dos está en 1620 psi de descarga.",
     [("F-201A", "presion_diferencial", 1.4, "kg/cm2", None, None),
      ("B-102", "presion_descarga_local", 1620, "psi", None, None)]),
    ("El filtro B medio 0,9 de diferencial, el pulmón está en 5 metros.",
     [("F-201B", "presion_diferencial", 0.9, "kg/cm2", None, None), ("TK-301", "nivel", 5, "m", None, None)]),
    ("La de la punta está en 112 de descarga. El de la derecha me dio 09 de diferencial.",
     [("B-101", "presion_descarga_local", 112, "kg/cm2", None, None),
      ("F-201B", "presion_diferencial", 0.9, "kg/cm2", None, None)]),
    ("La dos está en 1640 psi y de descarga el rodamiento medio 55 grados con la pistola hace un golpe teometálico.",
     [("B-102", "presion_descarga_local", 1640, "psi", None, None),
      ("B-102", "temperatura_rodamiento_ir", 55, "degC", None, None),
      ("B-102", "ruido", None, None, "hace un golpeteo metálico", None)]),
    ("La uno está en 114 kilos de descarga. El rodamiento medió 64 grados con la pistola.",
     [("B-101", "presion_descarga_local", 114, "kg/cm2", None, None),
      ("B-101", "temperatura_rodamiento_ir", 64, "degC", None, None)]),
    ("Este, la del medio está en 1610 psi, si de descarga. El rodamiento me dio 51 grados con la pistola.",
     [("B-102", "presion_descarga_local", 1610, "psi", None, None),
      ("B-102", "temperatura_rodamiento_ir", 51, "degC", None, None)]),
    # "psi" escuchado como "pese" y "el filtro A" como "el fito a", con "la bomba" a secas en el medio.
    ("La uno está en 112 kilogramos de descarga, el rodamiento medió 59 grados con la pistola, Ambient la bomba "
     "está en 1600 pese y de descarga, el fito a medio 1,3 de diferencial.",
     [("B-101", "presion_descarga_local", 112, "kg/cm2", None, None),
      ("B-101", "temperatura_rodamiento_ir", 59, "degC", None, None),
      (None, "presion_descarga_local", 1600, "psi", None, "equipo"),
      ("F-201A", "presion_diferencial", 1.3, "kg/cm2", None, None)]),
])
def test_transcripciones_de_whisper(catalogo, texto, esperado):
    assert filas(texto, catalogo) == esperado


def test_la_bomba_a_secas_queda_para_preguntar(catalogo):
    assert filas("La bomba está en mil seiscientos veinte psi de descarga.", catalogo) == [
        (None, "presion_descarga_local", 1620, "psi", None, "equipo")]
    assert filas("La bomba está parada en beserbat.", catalogo) == [
        (None, "estado", None, None, "parada, en reserva", "equipo")]


def test_un_diferencial_dicho_doce_es_uno_dos_y_hay_que_confirmarlo(catalogo):
    [f] = extraer("El de la derecha, medio, doce, de diferencial.", catalogo)
    assert (f.tag, f.valor, f.ambigua, f.dicho) == ("F-201B", 1.2, "valor", "doce")


def test_las_fotos_van_a_la_fila_que_corresponde(catalogo):
    resultado = extraer("La tres está en ciento diez bar de descarga. El filtro A me dio uno seis de diferencial, "
                        "le saqué foto al manómetro.", catalogo, ["/media/fotos/manometro_diferencial/02.jpg"])
    assert [(f.tag, f.foto) for f in resultado] == [("B-103", None), ("F-201A", "/media/fotos/manometro_diferencial/02.jpg")]


def test_una_condicion_insegura_sin_lecturas_no_arma_filas(catalogo):
    assert filas("¡Atención! Se rompió algo en la descarga de la uno, sale agua a presión para todos lados.", catalogo) == []

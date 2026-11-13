from datetime import datetime, timedelta, timezone

import pytest
from conftest import PLANTA
from planta_contrato import EventoSistema, LecturaSensor

from sistema.equipos import FuenteDatosPlanta
from sistema.sensores import DetectorSensores

INICIO = datetime(2026, 11, 18, 8, 0, tzinfo=timezone(timedelta(hours=-3)))


@pytest.fixture
def publicados():
    return []


@pytest.fixture
def detector(publicados):
    fuente = FuenteDatosPlanta(lambda: PLANTA)
    fuente.obtener()
    return DetectorSensores(fuente, publicados.append)


def serie(detector, tag, variable, valores, desde=INICIO, paso_min=1, calidad="buena"):
    for i, v in enumerate(valores):
        detector.lectura(LecturaSensor(tag=tag, variable=variable, valor=v, unidad="kg/cm2",
                                       ts=desde + timedelta(minutes=i * paso_min), calidad=calidad))


def test_un_pico_aislado_no_escala(detector, publicados):
    serie(detector, "B-101", "presion_descarga", [112] * 5 + [130, 131] + [112] * 20 + [126] * 3 + [112] * 10)
    assert publicados == []


def test_un_desvio_sostenido_escala_una_sola_vez(detector, publicados):
    serie(detector, "B-101", "presion_descarga", [112] + [127] * 30)
    [evento] = publicados
    EventoSistema.model_validate_json(evento.model_dump_json())
    assert (evento.tipo.value, evento.equipo, evento.origen, evento.nota_ids) == ("Escalado", "B-101", "sensor", [])
    assert "sobre la alarma alta (125)" in evento.resumen


def test_un_desvio_nuevo_despues_de_volver_es_otro_evento(detector, publicados):
    serie(detector, "F-201A", "presion_diferencial", [1.6] * 12 + [1.0] * 6 + [1.7] * 12)
    assert len(publicados) == 2 and publicados[0].id != publicados[1].id


def test_una_bomba_parada_no_es_presion_baja(detector, publicados):
    serie(detector, "B-103", "presion_descarga", [1.4] * 30)
    assert publicados == []


def test_presion_baja_con_la_bomba_en_marcha_si(detector, publicados):
    serie(detector, "B-103", "presion_descarga", [93] * 15)
    assert len(publicados) == 1 and "bajo la alarma baja (95)" in publicados[0].resumen


def test_sin_dato_no_cuenta(detector, publicados):
    serie(detector, "B-101", "presion_descarga", [None] * 20, calidad="sin_dato")
    assert publicados == []


def test_sin_datos_de_la_planta_no_hace_nada(publicados):
    detector = DetectorSensores(FuenteDatosPlanta(lambda: PLANTA), publicados.append)   # todavía no se leyeron
    serie(detector, "B-101", "presion_descarga", [130] * 20)
    assert publicados == []

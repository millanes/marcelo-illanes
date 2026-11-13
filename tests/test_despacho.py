import json

from sistema.despacho import Despachador
from sistema.reloj import RelojPlanta

LECTURA = {"tag": "B-102", "variable": "presion_descarga", "valor": 112.3, "unidad": "kg/cm2",
           "ts": "2026-11-16T15:21:20-03:00", "calidad": "buena"}
NOTA = {"id": "NOTA-00061", "ts": "2026-11-16T14:13:54-03:00", "operador": "Operador de prueba",
        "audio_url": "/media/audios/NOTA-00061.wav", "fotos": [], "recorrida_id": "REC-00012"}


def bytes_de(datos: dict) -> bytes:
    return json.dumps(datos).encode()


def test_una_lectura_actualiza_la_ultima_lectura_y_el_reloj():
    d = Despachador(RelojPlanta())
    d.recibir("planta/sensores/B-102/presion_descarga", bytes_de(LECTURA))
    assert d.lecturas[("B-102", "presion_descarga")].valor == 112.3
    assert d.reloj.ahora().isoformat() == "2026-11-16T15:21:20-03:00"
    assert d.en_cola == 0   # los sensores no pasan por la cola


def test_una_lectura_invalida_se_descarta_sin_romper():
    d = Despachador(RelojPlanta())
    d.recibir("planta/sensores/B-102/presion_descarga", b'{"tag": "B-102"}')
    d.recibir("planta/sensores/B-102/presion_descarga", b"no es json")
    assert d.lecturas == {}


def test_las_notas_van_a_la_cola():
    d = Despachador(RelojPlanta())
    d.recibir("planta/recorridas", bytes_de(NOTA))
    assert d.en_cola == 1


def test_la_nota_se_valida_y_llega_a_su_manejador():
    d = Despachador(RelojPlanta())
    recibidas = []
    d.suscribir("planta/recorridas", recibidas.append)
    d.procesar("planta/recorridas", bytes_de(NOTA))
    assert [n.id for n in recibidas] == ["NOTA-00061"]


def test_un_campo_que_no_esta_en_el_contrato_no_pasa():
    d = Despachador(RelojPlanta())
    recibidas = []
    d.suscribir("planta/recorridas", recibidas.append)
    d.procesar("planta/recorridas", bytes_de(NOTA | {"campo_de_mas": 1}))
    assert recibidas == []


def test_un_manejador_que_falla_no_frena_a_los_demas():
    d = Despachador(RelojPlanta())
    recibidas = []

    def romperse(_nota):
        raise RuntimeError("falla a propósito")

    d.suscribir("planta/recorridas", romperse)
    d.suscribir("planta/recorridas", recibidas.append)
    d.procesar("planta/recorridas", bytes_de(NOTA))
    assert len(recibidas) == 1


def test_registra_si_hay_energia():
    d = Despachador(RelojPlanta())
    d.procesar("planta/energia", bytes_de({"ts": "2026-11-16T15:00:00-03:00", "hay_energia": False}))
    assert d.hay_energia is False

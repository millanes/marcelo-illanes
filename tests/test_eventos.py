from datetime import datetime, timedelta, timezone

from planta_contrato import EventoSistema

from sistema.equipos import DatosPlanta
from sistema.eventos import eventos_del_parte
from sistema.extractor import Fila

TS = datetime(2026, 11, 18, 8, 40, tzinfo=timezone(timedelta(hours=-3)))


def eventos(filas, datos_planta, contexto=None):
    salida = eventos_del_parte("NOTA-00300", filas, DatosPlanta.desde_api(datos_planta).limites, TS, contexto)
    for e in salida:   # todos cumplen el contrato
        EventoSistema.model_validate_json(e.model_dump_json())
    return {(e.equipo, e.tipo.value) for e in salida}, {e.id for e in salida}


def test_lecturas_normales_son_sin_novedad(datos_planta):
    tipos, ids = eventos([Fila("B-101", "presion_descarga_local", 114, "kg/cm2"),
                          Fila("F-201B", "presion_diferencial", 0.9, "kg/cm2")], datos_planta)
    assert tipos == {("B-101", "SinNovedad"), ("F-201B", "SinNovedad")}
    assert ids == {"EV-NOTA-00300-B-101", "EV-NOTA-00300-F-201B"}


def test_una_observacion_anormal_escala_y_parada_en_reserva_no(datos_planta):
    tipos, _ = eventos([Fila("B-102", "ruido", observacion="hace un golpeteo metálico"),
                        Fila("B-103", "estado", observacion="parada, en reserva")], datos_planta)
    assert tipos == {("B-102", "Escalado"), ("B-103", "SinNovedad")}


def test_un_valor_fuera_de_limite_escala_aunque_venga_en_otra_unidad(datos_planta):
    # 1800 psi son 126,6 kg/cm2: por encima de la alarma alta de 125.
    tipos, _ = eventos([Fila("B-102", "presion_descarga_local", 1800, "psi"),
                        Fila("B-103", "presion_descarga_local", 110, "bar")], datos_planta)
    assert tipos == {("B-102", "Escalado"), ("B-103", "SinNovedad")}


def test_sin_equipos_es_una_recorrida_sin_novedad(datos_planta):
    tipos, ids = eventos([Fila("PLANTA", "recorrida", observacion="sin novedad")], datos_planta)
    assert tipos == {(None, "SinNovedad")} and ids == {"EV-NOTA-00300-SN"}


def test_el_escalado_lleva_su_contexto(datos_planta):
    salida = eventos_del_parte("NOTA-00300", [Fila("B-101", "goteo_empaquetadura", observacion="goteo por empaquetadura")],
                               DatosPlanta.desde_api(datos_planta).limites, TS,
                               {"B-101": "Fotos: /media/fotos/empaquetadura/01.jpg"})
    assert "goteo por empaquetadura" in salida[0].resumen and "/media/fotos/empaquetadura/01.jpg" in salida[0].resumen

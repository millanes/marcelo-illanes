import shutil
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from planta_falsa import MATERIALES, BuscadorFalso, PlantaFalsa, fragmento, herramientas
from pydantic import ValidationError

from sistema.herramientas import (DEFINICION, DEFINICIONES, EntradaBuscarManuales, EntradaCatalogoTareas,
                                  EntradaEquipos, EntradaHistorialOrdenes, EntradaHistorialSensores,
                                  EntradaMateriales, Herramientas, ruta_materiales)

HUSO = timezone(timedelta(hours=-3))

# Argumentos válidos y mínimos de cada tool.
MINIMOS = {
    "equipos": {},
    "historial_sensores": {"tag": "B-101", "variable": "vibracion"},
    "historial_ordenes": {},
    "materiales": {},
    "catalogo_tareas": {},
    "buscar_manuales": {"consulta": "a qué vibración dispara la triplex"},
}


@pytest.fixture
def planta() -> PlantaFalsa:
    return PlantaFalsa()


# --- Todas ------------------------------------------------------------------------------------------------------

def test_son_seis_tools_con_entrada_y_salida_estrictas():
    assert [d.nombre for d in DEFINICIONES] == list(MINIMOS)
    for d in DEFINICIONES:
        assert d.entrada.model_config["extra"] == "forbid"
        assert d.salida.model_config["extra"] == "forbid"
        assert d.descripcion and callable(getattr(Herramientas, d.nombre))
        assert all(campo.description for campo in d.entrada.model_fields.values()), d.nombre


@pytest.mark.parametrize("nombre", list(MINIMOS))
def test_un_argumento_de_mas_se_rechaza(planta, nombre):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DEFINICION[nombre].entrada.model_validate({**MINIMOS[nombre], "colado": 1})

    salida = herramientas(planta, BuscadorFalso()).ejecutar(nombre, {**MINIMOS[nombre], "colado": 1})

    assert salida.error.tipo == "entrada_invalida" and not salida.error.reintentable
    assert "colado: no es un argumento de esta tool" in salida.error.mensaje
    assert planta.pedidos == []


@pytest.mark.parametrize("nombre", list(MINIMOS))
def test_con_los_argumentos_minimos_contesta_sin_error(planta, nombre):
    assert herramientas(planta, BuscadorFalso()).ejecutar(nombre, MINIMOS[nombre]).error is None


def test_una_tool_que_no_existe_es_keyerror(planta):
    with pytest.raises(KeyError):
        herramientas(planta).ejecutar("borrar_todo", {})


def test_la_ruta_de_materiales_sale_del_entorno(monkeypatch, tmp_path):
    monkeypatch.setenv("MATERIALES_YAML", str(tmp_path / "otro.yaml"))
    assert ruta_materiales() == tmp_path / "otro.yaml"
    monkeypatch.delenv("MATERIALES_YAML")
    assert ruta_materiales().read_text(encoding="utf-8") == MATERIALES.read_text(encoding="utf-8")


# --- equipos ----------------------------------------------------------------------------------------------------

def test_equipos_trae_cada_equipo_con_sus_sensores_y_limites(planta):
    salida = herramientas(planta).equipos(EntradaEquipos())

    assert salida.planta == "Planta de Inyección PIA-3" and salida.inyeccion_programada_m3d == 2400
    assert [e.tag for e in salida.equipos] == ["B-101", "B-102", "B-103", "F-201A", "F-201B", "TK-301", "FT-401"]
    b101 = salida.equipos[0]
    assert b101.unidad_manometro == "kg/cm2" and "la de la punta" in b101.nombres
    assert [(s.variable, s.alarma_alta, s.alarma_baja, s.disparo) for s in b101.sensores] == [
        ("presion_descarga", 125, 95, None), ("vibracion", 7.1, None, 11.2)]


@pytest.mark.parametrize("entrada, tags", [
    (EntradaEquipos(nombre="la del medio"), ["B-102"]),
    (EntradaEquipos(nombre="el de la izquierda"), ["F-201A"]),
    (EntradaEquipos(tag="b101"), ["B-101"]),
    (EntradaEquipos(tipo="filtro"), ["F-201A", "F-201B"]),
    (EntradaEquipos(tipo="filtro", nombre="la dos"), []),
])
def test_equipos_filtra_por_tag_tipo_y_nombre_informal(planta, entrada, tags):
    assert [e.tag for e in herramientas(planta).equipos(entrada).equipos] == tags


@pytest.mark.parametrize("entrada, dice", [
    (EntradaEquipos(nombre="la bomba"), "hay que preguntar cuál"),
    (EntradaEquipos(nombre="la uno y la dos"), "más de un equipo: B-101, B-102"),
    (EntradaEquipos(tag="B-104"), "No hay ningún equipo B-104. Los equipos son: B-101"),
])
def test_equipos_lo_que_no_nombra_un_equipo_es_no_encontrado(planta, entrada, dice):
    salida = herramientas(planta).equipos(entrada)
    assert salida.error.tipo == "no_encontrado" and dice in salida.error.mensaje and salida.equipos == []


def test_equipos_un_tag_mal_formado_no_valida():
    with pytest.raises(ValidationError):
        EntradaEquipos(tag="la uno")


def test_equipos_reintenta_un_5xx(planta):
    planta.fallar("/api/planta", 503)
    salida = herramientas(planta).equipos(EntradaEquipos())
    assert salida.error is None and len(salida.equipos) == 7
    assert planta.rutas() == ["/api/planta", "/api/planta"]


@pytest.mark.parametrize("falla, tipo", [
    (503, "error_planta"),
    (httpx.ConnectError, "sin_conexion"),
    (httpx.ReadTimeout, "timeout"),
])
def test_equipos_con_la_planta_caida_devuelve_un_error_estructurado(planta, falla, tipo):
    planta.fallar("/api/planta", falla, falla, falla)

    salida = herramientas(planta).equipos(EntradaEquipos())

    assert salida.error.tipo == tipo and salida.error.reintentable and salida.equipos == []
    assert "3 intentos" in salida.error.mensaje
    assert salida.error.estado_http == (503 if falla == 503 else None)
    assert len(planta.pedidos) == 3


def test_la_planta_se_lee_una_vez_y_se_vuelve_a_pedir_si_fallo(planta):
    tools = herramientas(planta)
    planta.fallar("/api/planta", 500, 500, 500)
    assert tools.equipos(EntradaEquipos()).error.tipo == "error_planta"
    assert tools.equipos(EntradaEquipos()).error is None
    assert tools.equipos(EntradaEquipos(tipo="bomba")).error is None
    assert planta.rutas().count("/api/planta") == 4      # 3 que fallaron y 1 que anduvo


# --- historial_sensores -----------------------------------------------------------------------------------------

def test_historial_resume_la_ventana_y_reduce_la_serie(planta):
    entrada = EntradaHistorialSensores(tag="B-101", variable="vibracion", hasta="2026-11-12T17:00:00Z", horas=6)

    salida = herramientas(planta).historial_sensores(entrada)

    # La hora va en la de la planta (UTC-3), que es la que entiende el historian.
    pedido = planta.pedidos[-1]
    assert pedido.url.path == "/api/historian/B-101/vibracion"
    assert pedido.url.params["desde"] == "2026-11-12T08:00:00-03:00"
    assert pedido.url.params["hasta"] == "2026-11-12T14:00:00-03:00"
    assert salida.error is None and salida.tag == "B-101" and salida.sensor.alarma_alta == 7.1
    assert (salida.desde, salida.hasta) == (datetime(2026, 11, 12, 8, tzinfo=HUSO),
                                            datetime(2026, 11, 12, 14, tzinfo=HUSO))
    r = salida.resumen
    assert r.lecturas == 360                                      # 361 minutos, uno sin dato
    assert (r.minimo, r.ts_minimo) == (2.0, datetime(2026, 11, 12, 8, tzinfo=HUSO))
    assert (r.maximo, r.ts_maximo) == (8.0, datetime(2026, 11, 12, 9, 40, tzinfo=HUSO))
    assert (r.primera, r.ultima, r.tendencia) == (2.0, 5.6, "subiendo")
    assert (r.sobre_alarma_alta, r.bajo_alarma_baja) == (1, None)
    assert len(salida.tramos) == 24
    assert salida.tramos[0].desde == salida.desde and salida.tramos[-1].hasta == salida.hasta
    assert salida.tramos[6].maximo == 8.0 and salida.tramos[6].minimo < salida.tramos[6].promedio < 8.0


def test_historial_sin_hasta_termina_en_la_hora_de_la_planta(planta):
    salida = herramientas(planta).historial_sensores(EntradaHistorialSensores(tag="B-101", variable="vibracion"))
    assert planta.rutas()[1] == "/api/estado"
    assert planta.pedidos[-1].url.params["hasta"] == "2026-11-12T14:00:30-03:00"
    assert salida.desde == datetime(2026, 11, 12, 8, 0, 30, tzinfo=HUSO)


def test_historial_puede_usar_el_reloj_del_sistema(planta):
    tools = herramientas(planta, hora_planta=lambda: datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc))
    salida = tools.historial_sensores(EntradaHistorialSensores(tag="B-101", variable="vibracion", horas=1, tramos=6))
    assert "/api/estado" not in planta.rutas()
    assert salida.hasta == datetime(2026, 11, 12, 14, tzinfo=HUSO) and salida.resumen.lecturas == 60
    assert len(salida.tramos) == 6


def test_historial_normaliza_la_variable_y_el_tag():
    entrada = EntradaHistorialSensores(tag="b-101", variable="Vibración")
    assert (entrada.tag, entrada.variable) == ("B-101", "vibracion")
    # Lo que va en la ruta del historian no puede traer barras: queda un nombre que no existe.
    assert EntradaHistorialSensores(tag="B101", variable="vibracion/../../api").variable == "vibracion_api"


@pytest.mark.parametrize("argumentos", [
    {"tag": "B-101", "variable": "vibracion", "horas": 169},       # más de 7 días
    {"tag": "B-101", "variable": "vibracion", "horas": 0},
    {"tag": "B-101", "variable": "vibracion", "tramos": 121},
    {"tag": "B-101", "variable": "¿?"},
    {"tag": "B/101", "variable": "vibracion"},
    {"tag": "B-101"},
])
def test_historial_rechaza_ventanas_y_variables_invalidas(argumentos):
    with pytest.raises(ValidationError):
        EntradaHistorialSensores.model_validate(argumentos)


@pytest.mark.parametrize("tag, variable, dice", [
    ("B-101", "caudal", "B-101 no tiene sensor de caudal. Tiene: presion_descarga, vibracion."),
    ("TK-301", "nivel", "TK-301 no tiene sensores conectados"),
    ("B-109", "vibracion", "No hay ningún equipo B-109"),
])
def test_historial_de_un_sensor_que_no_existe_es_no_encontrado(planta, tag, variable, dice):
    salida = herramientas(planta).historial_sensores(EntradaHistorialSensores(tag=tag, variable=variable))
    assert salida.error.tipo == "no_encontrado" and dice in salida.error.mensaje
    assert not [r for r in planta.rutas() if r.startswith("/api/historian")]


def test_historial_reintenta_el_500_del_historian(planta):
    # Pasa de verdad: el historian da 500 si se lo lee mientras escribe ("deque mutated during iteration").
    planta.fallar("/api/historian", 500)
    salida = herramientas(planta).historial_sensores(EntradaHistorialSensores(tag="B-101", variable="vibracion"))
    assert salida.error is None and salida.resumen.lecturas > 0
    assert [r for r in planta.rutas() if r.startswith("/api/historian")] == ["/api/historian/B-101/vibracion"] * 2


def test_historial_un_4xx_no_se_reintenta_y_dice_por_que(planta):
    planta.fallar("/api/historian", 422)
    salida = herramientas(planta).historial_sensores(EntradaHistorialSensores(tag="B-101", variable="vibracion"))
    assert salida.error.tipo == "pedido_rechazado" and not salida.error.reintentable
    assert salida.error.estado_http == 422 and "falla de prueba" in salida.error.mensaje
    assert len([r for r in planta.rutas() if r.startswith("/api/historian")]) == 1


def test_historial_sin_lecturas_o_con_respuesta_rara(planta):
    tools = herramientas(planta)
    planta.historian = []
    vacia = tools.historial_sensores(EntradaHistorialSensores(tag="B-101", variable="vibracion"))
    assert vacia.error is None and vacia.resumen is None and vacia.tramos == []
    planta.historian = {"detail": "no es una lista"}
    rara = tools.historial_sensores(EntradaHistorialSensores(tag="B-101", variable="vibracion"))
    assert rara.error.tipo == "respuesta_invalida"


# --- historial_ordenes ------------------------------------------------------------------------------------------

def test_ordenes_las_mas_recientes_primero_hasta_el_limite(planta):
    salida = herramientas(planta).historial_ordenes(EntradaHistorialOrdenes(limite=2))

    assert salida.error is None and salida.total == 4
    assert [o.id for o in salida.ordenes] == ["OT-H-0003", "OT-26-0076"]
    simulador = salida.ordenes[0]
    assert (simulador.origen, simulador.descripcion, simulador.tipo) == ("simulador", "Corrección de falla F01", None)


def test_ordenes_de_un_equipo_entre_dos_dias(planta):
    entrada = EntradaHistorialOrdenes(equipo="b103", desde="2026-08-01", hasta="2026-08-04")

    salida = herramientas(planta).historial_ordenes(entrada)

    # Los días van completos y en la hora de la planta.
    assert dict(planta.pedidos[-1].url.params) == {
        "equipo": "B-103", "desde": "2026-08-01T00:00:00-03:00", "hasta": "2026-08-04T23:59:59-03:00"}
    [orden] = salida.ordenes
    assert (orden.id, orden.codigo_falla, orden.repuestos[0].codigo) == ("OT-26-0069", "F01", "TX-EMP-KIT")
    assert orden.fecha == datetime(2026, 8, 4, 15, 50, tzinfo=HUSO)


@pytest.mark.parametrize("entrada, ids", [
    (EntradaHistorialOrdenes(texto="empaq"), ["OT-26-0076", "OT-26-0069"]),
    (EntradaHistorialOrdenes(texto="VÁLVULAS"), ["OT-24-0001"]),
    (EntradaHistorialOrdenes(tarea="t-b-emp", limite=5), ["OT-H-0003", "OT-26-0076", "OT-26-0069"]),
    (EntradaHistorialOrdenes(codigo_falla="f01"), ["OT-26-0069"]),
])
def test_ordenes_filtran_por_texto_tarea_y_codigo_de_falla(planta, entrada, ids):
    salida = herramientas(planta).historial_ordenes(entrada)
    assert [o.id for o in salida.ordenes] == ids and salida.total == len(ids)


def test_ordenes_validan_fechas_y_equipo(planta):
    with pytest.raises(ValidationError, match="desde es posterior a hasta"):
        EntradaHistorialOrdenes(desde="2026-10-10", hasta="2026-10-01")
    with pytest.raises(ValidationError):
        EntradaHistorialOrdenes(limite=500)
    assert EntradaHistorialOrdenes(equipo="planta").equipo == "PLANTA"
    salida = herramientas(planta).historial_ordenes(EntradaHistorialOrdenes(equipo="B-104"))
    assert salida.error.tipo == "no_encontrado" and "/api/historial-ordenes" not in planta.rutas()


def test_ordenes_reintentan_un_5xx_y_si_no_contesta_es_error(planta):
    tools = herramientas(planta)
    planta.fallar("/api/historial-ordenes", 502)
    assert tools.historial_ordenes(EntradaHistorialOrdenes()).total == 4
    planta.fallar("/api/historial-ordenes", 502, 502, 502)
    salida = tools.historial_ordenes(EntradaHistorialOrdenes())
    assert salida.error.tipo == "error_planta" and salida.error.estado_http == 502 and salida.ordenes == []
    assert planta.rutas().count("/api/historial-ordenes") == 5


def test_una_orden_mal_armada_se_saltea(planta):
    planta.ordenes.append({"id": "OT-ROTA", "equipo": "B-101"})       # sin fecha
    salida = herramientas(planta).historial_ordenes(EntradaHistorialOrdenes())
    assert salida.error is None and "OT-ROTA" not in [o.id for o in salida.ordenes] and salida.total == 4


# --- materiales -------------------------------------------------------------------------------------------------

def test_materiales_es_la_lista_de_materiales_de_un_equipo(planta):
    salida = herramientas(planta).materiales(EntradaMateriales(equipo="F-201A"))

    assert [r.codigo for r in salida.repuestos] == ["FC-CART-25", "FC-JUNTA-TAPA", "INS-TRANS-P"]
    cartucho = salida.repuestos[0]
    assert (cartucho.costo_usd, cartucho.equipos) == (14, ["F-201A"])
    assert cartucho.stock == {"yacimiento": 96, "central": 600}
    assert planta.pedidos == []          # sale del archivo, no de la planta


@pytest.mark.parametrize("entrada, codigos", [
    (EntradaMateriales(codigo="tx-emp-kit"), ["TX-EMP-KIT"]),
    (EntradaMateriales(texto="Cartucho"), ["FC-CART-25", "FC-CART-10"]),
    (EntradaMateriales(equipo="B-101", con_stock_en="yacimiento"),
     ["TX-EMP-KIT", "TX-VAL-KIT", "TX-SELLO-CAR", "ACE-ISO220", "COR-5V-1800", "ACO-ELAST-90", "INS-TRANS-P"]),
])
def test_materiales_filtra_por_codigo_texto_y_stock(planta, entrada, codigos):
    assert [r.codigo for r in herramientas(planta).materiales(entrada).repuestos] == codigos


@pytest.mark.parametrize("entrada, dice", [
    (EntradaMateriales(equipo="TK-301"), "TK-301 no tiene lista de materiales"),
    (EntradaMateriales(codigo="350-EMP-03"), "No hay ningún repuesto 350-EMP-03"),
])
def test_materiales_lo_que_no_esta_es_no_encontrado(planta, entrada, dice):
    salida = herramientas(planta).materiales(entrada)
    assert salida.error.tipo == "no_encontrado" and dice in salida.error.mensaje


def test_materiales_sin_archivo_es_error_y_se_vuelve_a_leer(planta, tmp_path):
    ruta = tmp_path / "materiales.yaml"
    tools = herramientas(planta, materiales=ruta)

    sin_archivo = tools.materiales(EntradaMateriales())
    shutil.copy(MATERIALES, ruta)
    con_archivo = tools.materiales(EntradaMateriales())

    assert sin_archivo.error.tipo == "sin_datos" and str(ruta) in sin_archivo.error.mensaje
    assert con_archivo.error is None and len(con_archivo.repuestos) == 11


def test_materiales_con_el_archivo_roto_es_error(planta, tmp_path):
    ruta = tmp_path / "materiales.yaml"
    ruta.write_text("repuestos: [{codigo: X, nombre: sin costo}]\n", encoding="utf-8")
    assert herramientas(planta, materiales=ruta).materiales(EntradaMateriales()).error.tipo == "sin_datos"
    ruta.write_text("repuestos: [esto no cierra\n", encoding="utf-8")
    assert herramientas(planta, materiales=ruta).materiales(EntradaMateriales()).error.tipo == "sin_datos"


# --- catalogo_tareas --------------------------------------------------------------------------------------------

def test_tareas_de_un_equipo_con_repuestos_costo_y_stock(planta):
    salida = herramientas(planta).catalogo_tareas(EntradaCatalogoTareas(equipo="F-201B"))

    assert [(t.codigo, t.estandar) for t in salida.tareas] == [
        ("T-F-CAR", True), ("T-I-CAL", True), ("T-B-ROD", False), ("T-B-ALI", False), ("I-VIB", False),
        ("I-VAL", False)]
    cartuchos = salida.tareas[0]
    assert (cartuchos.detiene, cartuchos.horas, cartuchos.ejecutor) == (False, 3, "propio")
    [repuesto] = cartuchos.repuestos
    assert (repuesto.codigo, repuesto.cantidad, repuesto.costo_unitario_usd) == ("FC-CART-10", 48, 17)
    assert repuesto.stock == {"yacimiento": 40, "central": 600} and cartuchos.costo_repuestos_usd == 816
    assert planta.pedidos == []


@pytest.mark.parametrize("entrada, codigos", [
    (EntradaCatalogoTareas(estandar=False), ["T-B-ROD", "T-B-ALI", "I-VIB", "I-VAL"]),
    (EntradaCatalogoTareas(codigo="t-f-car"), ["T-F-CAR", "T-F-CAR"]),        # una por filtro
    (EntradaCatalogoTareas(texto="rodamientos"), ["T-B-ROD"]),
    (EntradaCatalogoTareas(equipo="B-102", estandar=True), ["T-B-EMP", "T-B-VAL", "T-B-ACE", "T-B-COR", "T-I-CAL"]),
])
def test_tareas_filtra_por_codigo_texto_y_si_es_estandar(planta, entrada, codigos):
    assert [t.codigo for t in herramientas(planta).catalogo_tareas(entrada).tareas] == codigos


def test_tareas_no_estandar_no_tienen_equipo_y_las_hace_la_contratista(planta):
    rodamientos = herramientas(planta).catalogo_tareas(EntradaCatalogoTareas(codigo="T-B-ROD")).tareas[0]
    assert (rodamientos.estandar, rodamientos.equipos, rodamientos.ejecutor, rodamientos.detiene) == (
        False, [], "contratista", True)


def test_tareas_errores(planta, tmp_path):
    sin_tareas = herramientas(planta).catalogo_tareas(EntradaCatalogoTareas(equipo="TK-301"))
    assert sin_tareas.error.tipo == "no_encontrado" and "TK-301 no tiene tareas estándar" in sin_tareas.error.mensaje
    sin_archivo = herramientas(planta, materiales=tmp_path / "no.yaml").catalogo_tareas(EntradaCatalogoTareas())
    assert sin_archivo.error.tipo == "sin_datos"


# --- buscar_manuales --------------------------------------------------------------------------------------------

def test_manuales_devuelve_los_fragmentos_con_su_cita(planta):
    largo = "x" * 3000
    buscador = BuscadorFalso([fragmento("TX350-D §3.2"), fragmento("TX350-C §3.2", vigente=False),
                              fragmento("MBP-4 §5.1", texto=largo, puntaje=0.123456)])
    entrada = EntradaBuscarManuales(consulta="a qué vibración dispara", k=3, equipo="b101")

    salida = herramientas(planta, buscador).buscar_manuales(entrada)

    assert buscador.llamados == [("a qué vibración dispara", 3, True, "B-101")]
    assert [f.cita for f in salida.fragmentos] == ["TX350-D §3.2", "MBP-4 §5.1"]   # la Rev. C vencida no sale
    tx, mbp = salida.fragmentos
    assert (tx.documento, tx.titulo, tx.vigente) == ("TX350-D", "Sección TX350-D §3.2", True)
    assert len(mbp.texto) == 1500 and mbp.texto.endswith("…") and mbp.puntaje == 0.1235


def test_manuales_con_las_revisiones_viejas_si_se_piden(planta):
    buscador = BuscadorFalso([fragmento("TX350-C §3.2", vigente=False)])
    salida = herramientas(planta, buscador).buscar_manuales(
        EntradaBuscarManuales(consulta="límites rev c", solo_vigentes=False))
    assert [f.cita for f in salida.fragmentos] == ["TX350-C §3.2"] and buscador.llamados[0][2] is False


def test_manuales_sin_buscador_es_error_estructurado(planta):
    salida = herramientas(planta).buscar_manuales(EntradaBuscarManuales(consulta="cartuchos"))
    assert salida.error.tipo == "sin_buscador" and salida.fragmentos == []


def test_manuales_mientras_se_prepara_el_indice(planta):
    buscador = BuscadorFalso()
    buscador.listo = False
    salida = herramientas(planta, buscador).buscar_manuales(EntradaBuscarManuales(consulta="cartuchos"))
    assert salida.error.tipo == "sin_buscador" and salida.error.reintentable and buscador.llamados == []


def test_manuales_reintenta_si_el_buscador_falla(planta):
    buscador = BuscadorFalso(fallas=1)
    salida = herramientas(planta, buscador).buscar_manuales(EntradaBuscarManuales(consulta="cartuchos"))
    assert salida.error is None and len(salida.fragmentos) == 1 and len(buscador.llamados) == 2


def test_manuales_se_rinde_despues_de_los_reintentos(planta):
    buscador = BuscadorFalso(fallas=5)
    salida = herramientas(planta, buscador).buscar_manuales(EntradaBuscarManuales(consulta="cartuchos"))
    assert salida.error.tipo == "error_buscador" and "se cortó la conexión" in salida.error.mensaje
    assert len(buscador.llamados) == 2


def test_manuales_con_timeout(planta):
    buscador = BuscadorFalso(trabar=True)
    try:
        salida = herramientas(planta, buscador, timeout_buscador_s=0.05).buscar_manuales(
            EntradaBuscarManuales(consulta="cartuchos"))
    finally:
        buscador.soltar.set()
    assert salida.error.tipo == "timeout" and salida.error.reintentable and len(buscador.llamados) == 1


def test_manuales_un_fragmento_incompleto_es_error_del_buscador(planta):
    incompleto = {"cita": "TX350-D §3.2", "texto": "sin documento ni puntaje"}
    salida = herramientas(planta, BuscadorFalso([incompleto])).buscar_manuales(EntradaBuscarManuales(consulta="x" * 5))
    assert salida.error.tipo == "error_buscador"

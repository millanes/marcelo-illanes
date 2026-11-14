"""RAG de manuales: metadatos de las trampas, filtros y ranking, con embeddings falsos y deterministas (sin bajar
ningún modelo). Si está RAG_TEST_DATABASE_URL, también prueba el backend pgvector contra ese Postgres."""

import hashlib
import json
import os

import numpy as np
import pytest

from sistema.rag import Buscador, Manuales, VectoresMemoria, leer_archivos, preparar, tokens
from sistema.rag.busqueda import Ajustes

INDICE = {"documentos": [
    {"id": "TX350-D", "titulo": "Bomba alternativa triplex Trimax TX-350", "revision": "D", "vigente": True,
     "tipo": "manual_fabricante", "emisor": "Trimax Industrial S.A.", "equipos": ["B-101", "B-102", "B-103"]},
    {"id": "TX350-C", "titulo": "Bomba alternativa triplex Trimax TX-350", "revision": "C", "vigente": False,
     "tipo": "manual_fabricante", "emisor": "Trimax Industrial S.A.", "equipos": ["B-101", "B-102", "B-103"]},
    {"id": "FC48-3", "titulo": "Filtro de cartuchos Filtrapat FC-48", "revision": "3", "vigente": True,
     "tipo": "manual_fabricante", "emisor": "Filtrapat S.R.L.", "equipos": ["F-201A", "F-201B"]},
    {"id": "MBP-4", "titulo": "Manual de buenas prácticas", "revision": "4", "vigente": True,
     "tipo": "manual_operadora", "emisor": "Gerencia de Operaciones",
     "equipos": ["B-101", "B-102", "B-103", "F-201A", "F-201B"]},
    {"id": "historial", "titulo": "Historial de órdenes de trabajo de PIA-3", "tipo": "historial"},
]}


def seccion(doc, numero, titulo, texto, vigente=True, clave=None):
    return {"id": f"{doc} §{numero}", "doc": doc, "numero": numero, "titulo": titulo, "clave": clave, "pagina": 1,
            "vigente": vigente, "texto": f"{numero} {titulo}\n{texto}"}


def orden(numero, equipo, tarea, aviso, trabajo):
    return {"id": f"historial {numero}", "doc": "historial", "numero": numero, "titulo": f"{numero} {equipo} {tarea}",
            "clave": None, "pagina": None, "vigente": True,
            "texto": f"{numero} · 2025-07-01 · {equipo} · {tarea} · propio\nAviso: {aviso}\nTrabajo: {trabajo}"}


LIMITES_D = "Vibración del cárter alarma 7,1 mm/s, disparo 11,2 mm/s. Temperatura de aceite alarma 80 °C."
LIMITES_C = "Vibración del cárter alarma 4,5 mm/s, disparo 7,1 mm/s. Temperatura de aceite alarma 85 °C."
LINEAS = [
    seccion("TX350-D", "3", "Especificaciones técnicas", "", clave="especificaciones"),
    seccion("TX350-D", "3.2", "Límites de operación", LIMITES_D, clave="limites"),
    seccion("TX350-D", "3.3", "Límites de operación de otros modelos de la familia",
            "Solo como referencia. Estos valores no aplican a la TX-350. TX-500 vibración alarma 4,5 mm/s.",
            clave="limites-otros"),
    seccion("TX350-D", "8.9", "TX-T07 Cavitación por succión insuficiente",
            "La bomba suena como si bombeara piedras. Presión de succión baja.", clave="falla-TX-T07"),
    seccion("TX350-D", "12", "Opcionales", "Verifique en la placa cuáles tiene su bomba.", clave="opcionales"),
    seccion("TX350-D", "12.1", "Lubricación forzada", "Bomba de engranajes que hace circular el aceite.",
            clave="op-lubricacion"),
    seccion("TX350-C", "3.2", "Límites de operación", LIMITES_C, vigente=False, clave="limites"),
    seccion("TX350-C", "8.9", "TX-T07 Cavitación por succión insuficiente",
            "La bomba suena como si bombeara piedras. Presión de succión baja.", vigente=False, clave="falla-TX-T07"),
    seccion("FC48-3", "8.3", "FC-T02 Cartucho roto o mal asentado", "La presión diferencial cae de golpe.",
            clave="falla-FC-T02"),
    seccion("MBP-4", "2.2", "Equipos y nombres", "TAG | Equipo | Nombres que se usan en campo\n"
            "B-102 | Bomba alternativa triplex 2 | la dos, la 2, la del medio\n"
            "B-103 | Bomba alternativa triplex 3 | la tres, la 3, la del fondo"),
    seccion("MBP-4", "4.4", "Planilla de papel del turno", "Abreviatura | Significa\nB2, Bba 2 | B-102\nB3, Bba 3 | B-103"),
    seccion("MBP-4", "6.1", "Resumen del catálogo", "Código | Falla | Fabricante | Tarea\n"
            "F08 | Cartucho roto (agua sin filtrar) | FC-T02 | T-F-CAR\n"
            "F09 | Cavitación por baja succión | TX-T07 | T-F-CAR\n"
            "F10 | Sensor congelado o con deriva | TX-T08 / FC-T04 | T-I-CAL"),
    seccion("MBP-4", "6.11", "F09 Cavitación por baja succión", "Ruido a piedras en el extremo hidráulico."),
    seccion("MBP-4", "17", "Secciones de otras plantas", "Estas secciones no aplican a PIA-3."),
    seccion("MBP-4", "17.1", "Bombas centrífugas", "Vibración de alarma de las centrífugas 4,5 mm/s."),
    orden("OT-25-0001", "B-102", "T-B-EMP", "pierde agua x el prensa", "cambio de empaquetadura, embolo ok"),
    orden("OT-25-0002", "B-103", "T-B-EMP", "pierde agua x el prensa", "cambio de empaquetadura, embolo ok"),
    orden("OT-25-0003", "F-201A", "T-F-CAR", "dif alto", "cambio de cartuchos x cavitacion en B2"),
]


class EmbebedorFalso:
    """Bolsa de palabras con hash: textos que comparten palabras quedan cerca. Determinista y sin modelo."""
    nombre = "falso"

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = dimension
        self.documentos_embebidos = 0

    def _vector(self, texto: str) -> np.ndarray:
        v = np.zeros(self.dimension, dtype=np.float32)
        for t in tokens(texto):
            v[int(hashlib.md5(t.encode()).hexdigest(), 16) % self.dimension] += 1.0
        norma = np.linalg.norm(v)
        return v / norma if norma else v

    def documentos(self, textos):
        self.documentos_embebidos += len(textos)
        return np.stack([self._vector(t) for t in textos])

    def consulta(self, texto):
        return self._vector(texto)


@pytest.fixture
def corpus():
    return preparar(LINEAS, INDICE)


@pytest.fixture
def buscador(corpus):
    return Buscador.en_memoria(corpus, EmbebedorFalso())


def citas(resultados):
    return [r["cita"] for r in resultados]


# --- Metadatos -----------------------------------------------------------------------------------------------------

def test_la_rev_c_queda_como_no_vigente(corpus):
    por_cita = {f.cita: f for f in corpus.fragmentos}
    assert not por_cita["TX350-C §3.2"].vigente
    assert por_cita["TX350-D §3.2"].vigente
    assert por_cita["TX350-D §3.2"].fabricante == "Trimax Industrial S.A."
    assert por_cita["TX350-D §3.2"].modelo == "TX-350"


def test_las_secciones_de_otros_modelos_y_plantas_no_aplican(corpus):
    por_cita = {f.cita: f for f in corpus.fragmentos}
    otro_modelo = por_cita["TX350-D §3.3"]
    assert not otro_modelo.aplica and "otro modelo" in otro_modelo.motivo and otro_modelo.otros == ("TX-500",)
    # El capítulo dice que sus secciones no aplican: la 17.1 lo hereda aunque no nombre ninguna planta.
    assert not por_cita["MBP-4 §17.1"].aplica and "otra planta" in por_cita["MBP-4 §17.1"].motivo
    assert not por_cita["TX350-D §12.1"].aplica and "opcional" in por_cita["TX350-D §12.1"].motivo
    assert por_cita["TX350-D §3.2"].aplica


def test_los_codigos_del_fabricante_se_traducen_con_la_tabla_del_mbp(corpus):
    assert corpus.traducir("TX-T07") == ["F09"]
    assert corpus.traducir("fc-t02") == ["F08"]
    assert corpus.traducir("F10") == ["TX-T08", "FC-T04"]
    por_cita = {f.cita: f for f in corpus.fragmentos}
    assert por_cita["TX350-D §8.9"].codigos == ("TX-T07", "F09")
    assert "F09 Cavitación por baja succión" in por_cita["TX350-D §8.9"].para_embeber
    assert por_cita["MBP-4 §6.11"].codigos == ("F09", "TX-T07")


def test_las_ordenes_llevan_su_equipo_y_los_que_nombran(corpus):
    por_cita = {f.cita: f for f in corpus.fragmentos}
    assert por_cita["historial OT-25-0001"].equipos == ("B-102",)
    assert por_cita["historial OT-25-0003"].equipos == ("F-201A", "B-102")    # "cavitacion en B2"
    assert por_cita["historial OT-25-0003"].fecha == "2025-07-01"


# --- Filtros y ranking ---------------------------------------------------------------------------------------------

def test_por_defecto_la_rev_c_no_sale(buscador):
    resultado = buscador.buscar("vibración del cárter alarma y disparo", k=10)
    assert resultado[0]["cita"] == "TX350-D §3.2"
    assert not any(r["documento"] == "TX350-C" for r in resultado)


def test_con_solo_vigentes_false_la_rev_c_sale_abajo_de_la_d(buscador):
    resultado = citas(buscador.buscar("bomba que suena a piedras con la succión baja", k=10, solo_vigentes=False))
    assert "TX350-C §8.9" in resultado
    assert resultado.index("TX350-D §8.9") < resultado.index("TX350-C §8.9")


def test_si_se_nombra_la_rev_c_no_se_castiga(buscador):
    nombrada = citas(buscador.buscar("límites de vibración de la Rev. C", k=10, solo_vigentes=False))
    sin_nombrar = citas(buscador.buscar("límites de vibración", k=10, solo_vigentes=False))
    assert nombrada.index("TX350-C §3.2") < nombrada.index("TX350-D §3.2")
    assert sin_nombrar.index("TX350-D §3.2") < sin_nombrar.index("TX350-C §3.2")


def test_las_secciones_de_otros_modelos_van_abajo(buscador):
    resultado = citas(buscador.buscar("límites de operación vibración alarma", k=10))
    assert resultado.index("TX350-D §3.2") < resultado.index("TX350-D §3.3")


def test_si_la_consulta_nombra_el_otro_modelo_no_se_castiga(buscador):
    resultado = citas(buscador.buscar("límites de operación de la TX-500", k=5))
    assert resultado[0] == "TX350-D §3.3"


def test_el_codigo_del_fabricante_trae_la_falla_de_la_planta(buscador):
    resultado = citas(buscador.buscar("apareció la falla TX-T07", k=3))
    assert "MBP-4 §6.11" in resultado and "TX350-D §8.9" in resultado


def test_el_codigo_de_la_planta_trae_la_seccion_del_fabricante(buscador):
    assert "TX350-D §8.9" in citas(buscador.buscar("falla F09", k=3))
    assert "FC48-3 §8.3" in citas(buscador.buscar("falla F08", k=3))


def test_el_equipo_pedido_prioriza_sus_ordenes_sin_descartar_las_otras(buscador):
    for equipo, suyo, otro in (("B-103", "historial OT-25-0002", "historial OT-25-0001"),
                               ("B-102", "historial OT-25-0001", "historial OT-25-0002")):
        resultado = citas(buscador.buscar("cambio de empaquetadura pierde agua", k=20, equipo=equipo))
        assert resultado.index(suyo) < resultado.index(otro)
    # La orden del filtro que nombra a la B-102 ("cavitacion en B2") también es de la B-102.
    resultado = buscador.buscar("cavitación", k=20, equipo="B-102")
    assert "historial OT-25-0003" in citas(resultado)
    assert any(r["documento"] == "FC48-3" for r in buscador.buscar("presión diferencial", k=20, equipo="B-102"))


def test_el_equipo_nombrado_sube_sus_ordenes(buscador):
    resultado = citas(buscador.buscar("la dos pierde por el prensa, ¿qué se cambió?", k=5))
    assert resultado.index("historial OT-25-0001") < resultado.index("historial OT-25-0002")
    resultado = citas(buscador.buscar("B3 pierde por el prensa, ¿qué se cambió?", k=5))
    assert resultado.index("historial OT-25-0002") < resultado.index("historial OT-25-0001")


def test_si_pregunta_por_lo_que_paso_suben_las_ordenes(corpus):
    neutro = Buscador.en_memoria(corpus, EmbebedorFalso(), ajustes=Ajustes(pasado=1.0))
    con_pasado = Buscador.en_memoria(corpus, EmbebedorFalso(), ajustes=Ajustes(pasado=3.0))
    consulta = "¿alguna vez hubo cavitación por filtro?"
    assert con_pasado.analizar(consulta).pasado and not con_pasado.analizar("cavitación por filtro").pasado
    antes = citas(neutro.buscar(consulta, k=10)).index("historial OT-25-0003")
    assert citas(con_pasado.buscar(consulta, k=10)).index("historial OT-25-0003") <= antes


def test_la_lista_de_precios_que_corresponde_al_anio():
    lineas = [seccion("CT-SVP-2025", "2.1", "Precios vigentes 2025-2026", "Análisis de vibraciones USD 320."),
              seccion("CT-SVP-2025", "2.5", "Precios del contrato anterior CT-SVP-2022",
                      "Solo para certificar trabajos iniciados antes del 1 de enero de 2025. No aplican a trabajos "
                      "nuevos.\nAnálisis de vibraciones USD 280.")]
    indice = {"documentos": [{"id": "CT-SVP-2025", "titulo": "Contrato", "tipo": "contrato", "vigente": True}]}
    corpus = preparar(lineas, indice)
    por_cita = {f.cita: f for f in corpus.fragmentos}
    assert por_cita["CT-SVP-2025 §2.1"].periodo == (2025, 2026)
    assert por_cita["CT-SVP-2025 §2.5"].periodo[1] == 2024 and not por_cita["CT-SVP-2025 §2.5"].aplica
    buscador = Buscador.en_memoria(corpus, EmbebedorFalso())
    assert citas(buscador.buscar("precio del análisis de vibraciones", k=2))[0] == "CT-SVP-2025 §2.1"
    assert citas(buscador.buscar("precio del análisis de vibraciones de un trabajo de 2024", k=2))[0] == \
        "CT-SVP-2025 §2.5"


def test_cada_resultado_trae_cita_y_metadatos(buscador):
    r = buscador.buscar("succión baja", k=1)[0]
    for campo in ("cita", "documento", "titulo", "texto", "vigente", "puntaje", "fabricante", "equipos", "seccion",
                  "aplica", "motivo", "codigos", "modelo"):
        assert campo in r
    assert r["puntaje"] > 0


def test_sin_embeddings_sigue_con_bm25(corpus):
    class Roto(EmbebedorFalso):
        def consulta(self, texto):
            raise RuntimeError("sin modelo")

    buscador = Buscador.en_memoria(corpus, Roto())
    assert buscador.buscar("presión diferencial cae de golpe", k=1)[0]["cita"] == "FC48-3 §8.3"
    assert Buscador(corpus, None, None).buscar("presión diferencial cae de golpe", k=1)[0]["cita"] == "FC48-3 §8.3"


def test_los_vectores_en_memoria_respetan_solo_vigentes(corpus):
    embebedor = EmbebedorFalso()
    vectores = VectoresMemoria(corpus.fragmentos, embebedor.documentos([f.para_embeber for f in corpus.fragmentos]))
    todos = vectores.parecidos(embebedor.consulta("límites de operación"), 100, False)
    vigentes = vectores.parecidos(embebedor.consulta("límites de operación"), 100, True)
    assert any(c.startswith("TX350-C") for c, _ in todos)
    assert not any(c.startswith("TX350-C") for c, _ in vigentes) and len(vigentes) == len(todos) - 2
    assert [s for _, s in todos] == sorted((s for _, s in todos), reverse=True)


def test_tokens_conserva_los_codigos():
    assert {"tx-t07", "t07"} <= set(tokens("Falla TX-T07"))
    assert tokens("Cigüeñal, ¿qué presión?") == ["ciguen", "presio"]


# --- Indexado al arrancar ------------------------------------------------------------------------------------------

FRAGMENTOS = "\n".join(json.dumps(linea, ensure_ascii=False) for linea in LINEAS)


class BaseFalsa:
    """Lo que hace BasePgvector, en memoria."""

    def __init__(self):
        self.guardado = None
        self.guardados = 0
        self.hay_base = True

    def disponible(self):
        return self.hay_base

    def estado(self):
        return self.guardado

    def guardar(self, corpus, matriz, fragmentos, indice, modelo, segundos):
        from sistema.rag.postgres import huella
        self.guardados += 1
        self.guardado = {"huella": huella(fragmentos, indice, modelo), "modelo": modelo, "fragmentos": len(matriz),
                         "archivo_fragmentos": fragmentos, "archivo_indice": indice, "indexado": "hoy"}
        return True


def manuales(descargar, embebedor=None):
    m = Manuales(descargar, "postgresql://nadie", embebedor or EmbebedorFalso())
    m.base = BaseFalsa()
    return m


def planta(archivos):
    def descargar(ruta):
        if archivos is None:
            raise ConnectionError("la planta no contesta")
        return archivos[ruta].encode("utf-8")
    return descargar


ARCHIVOS = {"/documentos/fragmentos.jsonl": FRAGMENTOS, "/documentos/indice.json": json.dumps(INDICE)}


def test_indexa_una_vez_y_no_re_indexa_si_no_cambio_nada():
    embebedor = EmbebedorFalso()
    m = manuales(planta(ARCHIVOS), embebedor)
    assert not m.listo and m.buscar("succión") == []
    assert m.preparar()
    assert m.listo and m.base.guardados == 1 and embebedor.documentos_embebidos == len(LINEAS)
    assert m.preparar()
    assert m.base.guardados == 1 and embebedor.documentos_embebidos == len(LINEAS)


def test_re_indexa_si_cambian_los_documentos_o_el_modelo():
    m = manuales(planta(ARCHIVOS))
    m.preparar()
    otros = dict(ARCHIVOS, **{"/documentos/fragmentos.jsonl": FRAGMENTOS + "\n" + json.dumps(
        orden("OT-26-0001", "B-101", "T-B-COR", "correas", "cambio de correas"))})
    m._descargar = planta(otros)
    m.preparar()
    assert m.base.guardados == 2
    m.embebedor.nombre = "otro-modelo"
    m.preparar()
    assert m.base.guardados == 3


def test_sin_planta_usa_el_indice_guardado():
    m = manuales(planta(ARCHIVOS))
    m.preparar()
    sin_planta = manuales(planta(None))
    sin_planta.base = m.base
    assert sin_planta.preparar()
    assert sin_planta.buscador.corpus.fragmentos[0].cita == "TX350-D §3"
    assert not manuales(planta(None)).preparar()     # sin planta y sin nada guardado


def test_sin_base_no_embebe_nada():
    embebedor = EmbebedorFalso()
    m = manuales(planta(ARCHIVOS), embebedor)
    m.base.hay_base = False
    assert not m.preparar() and not m.listo
    assert embebedor.documentos_embebidos == 0      # se reintenta después, sin gastar CPU en vectores perdidos
    m.base.hay_base = True
    assert m.preparar() and m.listo


# --- Postgres de verdad (opcional) ---------------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("RAG_TEST_DATABASE_URL"), reason="sin RAG_TEST_DATABASE_URL")
def test_pgvector_da_lo_mismo_que_la_memoria():
    from sistema.rag import BasePgvector, VectoresPostgres

    corpus = leer_archivos(FRAGMENTOS, json.dumps(INDICE))
    embebedor = EmbebedorFalso()
    matriz = embebedor.documentos([f.para_embeber for f in corpus.fragmentos])
    base = BasePgvector(os.environ["RAG_TEST_DATABASE_URL"])
    assert base.guardar(corpus, matriz, FRAGMENTOS, json.dumps(INDICE), embebedor.nombre, 0.0)
    en_base = Buscador(corpus, embebedor, VectoresPostgres(base))
    en_memoria = Buscador.en_memoria(corpus, embebedor, matriz)
    for consulta, vigentes, equipo in [("succión baja, ruido a piedras", True, None), ("límites", False, None),
                                       ("pierde agua x el prensa", True, "B-103"), ("falla F09", True, None)]:
        assert citas(en_base.buscar(consulta, 10, vigentes, equipo)) == citas(en_memoria.buscar(consulta, 10, vigentes,
                                                                                                 equipo))
    assert base.estado()["fragmentos"] == len(LINEAS)

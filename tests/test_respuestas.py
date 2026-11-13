import pytest

from sistema.equipos import Catalogo
from sistema.respuestas import interpretar


@pytest.fixture
def catalogo(datos_planta) -> Catalogo:
    return Catalogo.desde_api(datos_planta)


@pytest.mark.parametrize("texto", ["Sí, confirmo.", "Está bien, confirmo.", "Correcto, dale.", "Sí, así es. Confirmo.",
                                   "Perfecto, confirmo."])
def test_confirmaciones(catalogo, texto):
    assert interpretar(texto, catalogo).tipo == "confirma"


def test_corrige_un_valor(catalogo):
    r = interpretar("No. La uno estaba en ciento quince kilos, no en ciento doce. Corregilo y te confirmo.", catalogo)
    assert r.tipo == "corrige"
    [c] = r.correcciones
    assert (c.tipo, c.tag, c.valor, c.unidad, c.anterior) == ("valor", "B-101", 115, "kg/cm2", 112)


def test_corrige_un_decimal_y_un_equipo(catalogo):
    r = interpretar("No. El filtro b estaba en uno coma ocho kilos, no en uno coma dos. Eso de mil seiscientos veinte "
                    "era de la dos, no de la uno. Y hay más cosas, revisalo todo.", catalogo)
    valor, equipo = r.correcciones
    assert (valor.tipo, valor.tag, valor.valor, valor.anterior) == ("valor", "F-201B", 1.8, 1.2)
    assert (equipo.tipo, equipo.tag, equipo.valor, equipo.tag_anterior) == ("equipo", "B-102", 1620, "B-101")
    assert r.hay_mas


def test_unidad_falta_y_sobra(catalogo):
    r = interpretar("No. Lo de la dos es en psi, no en kilos. Te faltó la uno, que estaba en cuarenta y cinco gotas "
                    "por minuto. Arreglá eso.", catalogo)
    unidad, falta = r.correcciones
    assert (unidad.tipo, unidad.tag, unidad.unidad) == ("unidad", "B-102", "psi")
    assert (falta.tipo, falta.tag, falta.valor, falta.unidad) == ("falta", "B-101", 45, "gotas/min")
    [sobra] = interpretar("No. Lo de el filtro A no lo dije. Con eso está.", catalogo).correcciones
    assert (sobra.tipo, sobra.tag) == ("sobra", "F-201A")


@pytest.mark.parametrize("texto, tag", [("La dos.", "B-102"), ("Era la dos.", "B-102"),
                                        ("La correcta es el filtro B.", "F-201B"), ("Ninguna de esas, era la tres.", "B-103")])
def test_elige_entre_opciones(catalogo, texto, tag):
    r = interpretar(texto, catalogo)
    assert (r.tipo, r.tag) == ("elige", tag)


@pytest.mark.parametrize("texto", ["No te entendí, ¿me lo repetís?", "¿De qué nota me hablás? No la tengo.",
                                   "¿Confirmar qué? Mostrame lo que anotaste."])
def test_no_entendio(catalogo, texto):
    assert interpretar(texto, catalogo).tipo == "no_entiende"

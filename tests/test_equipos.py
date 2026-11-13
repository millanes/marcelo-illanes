import pytest

from sistema.equipos import Catalogo


@pytest.fixture
def catalogo(datos_planta) -> Catalogo:
    return Catalogo.desde_api(datos_planta)


@pytest.mark.parametrize("texto, tags", [
    ("La de la punta está en ciento catorce de descarga.", ["B-101"]),
    ("El de la izquierda me dio uno coma cuatro de diferencial.", ["F-201A"]),
    ("El filtro B me dio uno ocho.", ["F-201B"]),
    ("La del portón pierde por la empaquetadura.", ["B-101"]),
    ("La 1 está en 112.", ["B-101"]),
    ("El pulmón está en cinco con cuatro metros.", ["TK-301"]),
    ("La tres está parada, en reserva. La dos está en mil seiscientos treinta.", ["B-103", "B-102"]),
    ("Revisé la B-102.", ["B-102"]),
])
def test_resuelve_los_nombres_informales(catalogo, texto, tags):
    assert [m.tag for m in catalogo.menciones(texto)] == tags


@pytest.mark.parametrize("texto", [
    "La bomba está en mil seiscientos veinte psi de descarga.",      # "la bomba" a secas: hay que preguntar
    "Hay olor a gas fuerte del lado del portón.",                    # el portón es un lugar, no "la del portón"
    "Recorrida sin novedad.",
])
def test_no_inventa_equipos(catalogo, texto):
    assert catalogo.menciones(texto) == []
    assert catalogo.unico(texto) is None


def test_unico_solo_si_nombra_un_equipo(catalogo):
    assert catalogo.unico("La uno, o sea la de la punta, está en 112.") == "B-101"
    assert catalogo.unico("La uno y la dos están en 112.") is None


def test_guarda_la_unidad_del_manometro_local(catalogo):
    assert catalogo.equipos["B-102"].unidad_manometro == "psi"
    assert catalogo.equipos["F-201A"].unidad_manometro is None

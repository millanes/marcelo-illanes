import pytest

from sistema.numeros import leer, preparar
from sistema.texto import normalizar


def numero(texto: str) -> float | None:
    return leer(normalizar(preparar(texto)).split())


@pytest.mark.parametrize("texto, valor", [
    ("ciento catorce", 114), ("mil seiscientos cuarenta", 1640), ("cincuenta y cinco", 55), ("doce", 12),
    ("cien", 100), ("dos mil", 2000), ("1620", 1620), ("1.640", 1640),
    ("uno coma cuatro", 1.4), ("uno con cuatro", 1.4), ("uno cuatro", 1.4), ("1,4", 1.4), ("1.4", 1.4),
    ("uno y medio", 1.5), ("1 y medio", 1.5), ("cero nueve", 0.9), ("09", 0.9), ("0,9", 0.9),
    ("cinco con cuatro", 5.4), ("uno", 1), ("cero coma ocho", 0.8),
])
def test_lee_numeros_dichos(texto, valor):
    assert numero(texto) == pytest.approx(valor)


@pytest.mark.parametrize("texto", ["", "descarga", "uno dos tres", "y"])
def test_lo_que_no_es_un_numero_da_none(texto):
    assert numero(texto) is None

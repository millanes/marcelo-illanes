import pytest

from sistema.seguridad import detectar


def codigos(texto: str) -> list[str]:
    return [h.codigo for h in detectar(texto)]


# Las cuatro condiciones como las dicta el operador de la planta, y variantes como las puede escribir Whisper
# o decir una persona en campo.
@pytest.mark.parametrize("texto, esperado", [
    ("¡Atención! Se rompió algo en la descarga de la uno, sale agua a presión para todos lados, me alejé. "
     "Saqué foto desde lejos.", ["S1"]),
    ("Sale agua apresión por la brida de la dos.", ["S1"]),
    ("Reventó la manguera de la descarga, tira agua con mucha presión.", ["S1"]),
    ("Hay olor a gas fuerte del lado del portón, viene del lado del separador.", ["S2"]),
    ("Huele raro, como a gas, cerca del separador.", ["S2"]),
    ("Sale humo del motor de la dos, hay olor a quemado.", ["S3"]),
    ("Atención, la tres está en llamas.", ["S3"]),
    ("Mi compañero se golpeó la mano con la llave en el skid de filtros, está sangrando bastante.", ["S4"]),
    ("mi companero se golpeo la mano, esta sangrando", ["S4"]),
    ("Hay un herido en el skid de bombas.", ["S4"]),
    ("Sale humo del tablero y el ayudante se quemó el brazo.", ["S3", "S4"]),
])
def test_detecta_condiciones_inseguras(texto, esperado):
    assert codigos(texto) == esperado


# Notas normales de la recorrida: no son condiciones inseguras y cada falsa alarma resta.
@pytest.mark.parametrize("texto", [
    "La uno chorrea por la empaquetadura, le saqué foto.",
    "La dos pierde bastante por la empaquetadura, le saqué foto.",
    "El pulmón está en tres coma dos metros, está rebalsando a la pileta.",
    "La del medio está en mil seiscientos treinta de descarga, hace un golpeteo metálico.",
    "La tres está disparada, no arranca.",
    "En la uno, las correas están flojas, chillan, saqué foto.",
    "El rodamiento me dio setenta y ocho grados con la pistola.",
    "Casi no tiene aceite en el visor.",
    "Recorrida sin novedad, todo normal.",
    "La de la punta está en ciento doce kilos de descarga.",
])
def test_las_notas_normales_no_son_alertas(texto):
    assert codigos(texto) == []


# Frases que se parecen a una condición insegura pero no lo son.
@pytest.mark.parametrize("texto", [
    "No hay olor a gas.",
    "Sin humo ni olor a quemado, todo normal.",
    "Nadie se lastimó la mano, quedó todo bien.",
    "No hay heridos.",
    "Se cortó la luz un rato y volvió.",
    "Se me cayó la llave, ya la levanté.",
    "Sangré la línea del filtro A.",
    "Si podés me llamás cuando llegue el repuesto.",
    "La bomba de la red de incendio está parada, en reserva.",
    "El agua sale a ciento diez de presión.",
    "Revisé el separador de gas, está bien.",
])
def test_no_confunde_frases_parecidas(texto):
    assert codigos(texto) == []


def test_una_negacion_no_tapa_lo_que_viene_despues():
    assert codigos("No hay olor a gas, pero sale humo del motor de la dos.") == ["S3"]


def test_devuelve_la_oracion_como_evidencia():
    [h] = detectar("La tres anda bien. Sale humo del motor de la dos.")
    assert h.oracion == "sale humo del motor de la dos"

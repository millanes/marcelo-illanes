import io
import wave

import pytest

# Lo mismo que devuelve GET /api/planta, recortado a lo que usa el sistema.
PLANTA = {"equipos": [
    {"tag": "B-101", "tipo": "bomba", "nombres": ["la uno", "la 1", "la de la punta", "la del portón", "bomba uno"],
     "manometro_local": {"unidad": "kg/cm2"}},
    {"tag": "B-102", "tipo": "bomba", "nombres": ["la dos", "la 2", "la del medio", "bomba dos"],
     "manometro_local": {"unidad": "psi"}},
    {"tag": "B-103", "tipo": "bomba", "nombres": ["la tres", "la 3", "la del fondo", "la de reserva", "bomba tres"],
     "manometro_local": {"unidad": "bar"}},
    {"tag": "F-201A", "tipo": "filtro", "nombres": ["el filtro A", "el primario", "el de la izquierda", "el grueso"]},
    {"tag": "F-201B", "tipo": "filtro", "nombres": ["el filtro B", "el secundario", "el de la derecha", "el fino"]},
    {"tag": "TK-301", "tipo": "tanque", "nombres": ["el pulmón", "el tanque", "el tanque pulmón"]},
    {"tag": "FT-401", "tipo": "caudalimetro", "nombres": ["el caudalímetro", "el colector"]},
], "sensores": [
    *({"tag": t, "variable": "presion_descarga", "unidad": "kg/cm2", "alarma_alta": 125, "alarma_baja": 95}
      for t in ("B-101", "B-102", "B-103")),
    *({"tag": t, "variable": "presion_diferencial", "unidad": "kg/cm2", "alarma_alta": 1.5, "alarma_baja": 0.15}
      for t in ("F-201A", "F-201B")),
]}


def wav(segundos: float = 1.0, tasa: int = 16000, canales: int = 1, bytes_por_muestra: int = 2) -> bytes:
    """Un WAV de silencio con el formato pedido."""
    salida = io.BytesIO()
    with wave.open(salida, "wb") as w:
        w.setnchannels(canales)
        w.setsampwidth(bytes_por_muestra)
        w.setframerate(tasa)
        w.writeframes(b"\x00" * int(segundos * tasa) * canales * bytes_por_muestra)
    return salida.getvalue()


@pytest.fixture
def datos_planta() -> dict:
    return PLANTA

"""El historial de sensores de la planta (GET /api/historian): rango de los últimos 45 días, para pedir
confirmación de un valor raro, y tendencia de las últimas horas, como contexto de un escalado."""

from __future__ import annotations

import logging
import statistics
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from .planta import ClientePlanta

log = logging.getLogger(__name__)

NOMBRE = {"presion_descarga": "presión de descarga", "presion_diferencial": "diferencial",
          "temperatura_aceite": "temperatura de aceite", "vibracion": "vibración"}


def _valores(muestras: list[dict]) -> list[float]:
    return [m["valor"] for m in muestras if m.get("valor") is not None]


class Historian:
    def __init__(self, planta: ClientePlanta, dias: int = 45, espera_tras_falla_s: float = 300.0) -> None:
        self._planta = planta
        self.dias = dias
        self._rangos: dict[tuple[str, str], tuple[float, float]] = {}
        # Si el historial falló, no se vuelve a pedir enseguida: cada borrador esperaría los reintentos.
        self._fallas: dict[tuple[str, str], float] = {}
        self.espera_tras_falla_s = espera_tras_falla_s
        self._candado = threading.Lock()

    def _serie(self, tag: str, variable: str, desde: datetime, hasta: datetime) -> list[float]:
        return _valores(self._planta.historian(tag, variable, quote(desde.isoformat()), quote(hasta.isoformat())))

    def rango(self, tag: str, variable: str, hasta: datetime) -> tuple[float, float] | None:
        """Mínimo y máximo de los últimos 45 días con el equipo en marcha (sin las horas parado)."""
        clave = (tag, variable)
        with self._candado:
            if clave in self._rangos:
                return self._rangos[clave]
            if time.monotonic() - self._fallas.get(clave, -1e9) < self.espera_tras_falla_s:
                return None
        try:
            valores = self._serie(tag, variable, hasta - timedelta(days=self.dias), hasta)
        except Exception as e:
            log.warning("No se pudo leer el historial de %s %s: %s", tag, variable, str(e).splitlines()[0])
            with self._candado:
                self._fallas[clave] = time.monotonic()
            return None
        if not valores:
            return None
        mediana = statistics.median(valores)
        en_marcha = [v for v in valores if v >= 0.5 * mediana] or valores
        rango = (min(en_marcha), max(en_marcha))
        with self._candado:
            self._rangos[clave] = rango
        return rango

    def precargar(self, pares: list[tuple[str, str]], hasta: datetime) -> None:
        """Lee los rangos en un hilo aparte, para que el primer borrador no los espere."""
        def leer() -> None:
            for tag, variable in pares:
                self.rango(tag, variable, hasta)
            log.info("Rangos históricos cargados: %d series", len(self._rangos))
        threading.Thread(target=leer, name="historian", daemon=True).start()

    def tendencia(self, tag: str, variable: str, hasta: datetime, horas: int = 6) -> str | None:
        """"vibración últimas 6 h: 2,1 a 3,4 (prom. 2,6), subiendo", o None si no hay datos."""
        try:
            valores = self._serie(tag, variable, hasta - timedelta(hours=horas), hasta)
        except Exception as e:
            log.warning("No se pudo leer la tendencia de %s %s: %s", tag, variable, e)
            return None
        if len(valores) < 2:
            return None
        cuarto = max(1, len(valores) // 4)
        inicio, fin = statistics.mean(valores[:cuarto]), statistics.mean(valores[-cuarto:])
        cambio = (fin - inicio) / abs(inicio) if inicio else 0.0
        sentido = "subiendo" if cambio > 0.05 else "bajando" if cambio < -0.05 else "estable"
        return (f"{NOMBRE.get(variable, variable)} últimas {horas} h: {min(valores):.1f} a {max(valores):.1f} "
                f"(prom. {statistics.mean(valores):.1f}), {sentido}")

"""Cliente HTTP de la planta (http://sim:8100): catálogo de equipos y descarga de audios y fotos.

La planta está en la red local: no pasa por el proxy (NO_PROXY) y anda aunque se caiga el enlace. Cada
pedido tiene timeout y reintenta ante errores de red o respuestas 5xx; un 4xx no se reintenta.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class ClientePlanta:
    def __init__(self, url: str, timeout_s: float = 10.0, reintentos: int = 2, espera_s: float = 0.5,
                 transporte: httpx.BaseTransport | None = None) -> None:
        self._cliente = httpx.Client(base_url=url, timeout=timeout_s, transport=transporte,
                                     trust_env=transporte is None)
        self.reintentos, self.espera_s = reintentos, espera_s

    def _get(self, ruta: str) -> httpx.Response:
        for intento in range(self.reintentos + 1):
            try:
                respuesta = self._cliente.get(ruta)
                respuesta.raise_for_status()
                return respuesta
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500 or intento == self.reintentos:
                    raise
                error: Exception = e
            except httpx.TransportError as e:
                if intento == self.reintentos:
                    raise
                error = e
            log.warning("GET %s falló (%s); reintento %d", ruta, error, intento + 1)
            time.sleep(self.espera_s * (intento + 1))
        raise AssertionError("inalcanzable")

    def planta(self) -> dict:
        """Equipos, nombres informales y límites de alarma (GET /api/planta)."""
        return self._get("/api/planta").json()

    def historian(self, tag: str, variable: str, desde: str, hasta: str) -> list[dict]:
        """Muestras de un sensor, una por minuto: [{"ts": ..., "valor": ...}] (GET /api/historian)."""
        return self._get(f"/api/historian/{tag}/{variable}?desde={desde}&hasta={hasta}").json()

    def descargar(self, url: str) -> bytes:
        """Un audio o una foto servidos por la planta, con la ruta que viene en el mensaje (/media/...)."""
        return self._get(url).content

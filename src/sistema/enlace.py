"""¿Hay enlace con la base?

La red de planta no tiene salida a internet: lo único que sale es el proxy `enlace` (HTTPS_PROXY), que
corta las conexiones cuando se cae el radioenlace. Para saberlo se hace un pedido corto a internet a
través del proxy: cualquier respuesta HTTP, aunque sea un error, quiere decir que hay enlace.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class SondaEnlace:
    def __init__(self, url: str, timeout_s: float = 5.0, transporte: httpx.BaseTransport | None = None) -> None:
        self.url = url
        # Sin transporte propio (los tests traen uno), httpx toma HTTPS_PROXY y NO_PROXY del entorno.
        self._cliente = httpx.Client(timeout=timeout_s, transport=transporte, trust_env=transporte is None)
        self.ultimo: bool | None = None

    def probar(self) -> bool:
        try:
            self._cliente.head(self.url)
            ok = True
        except httpx.HTTPError as e:
            ok = False
            if self.ultimo is not False:
                log.warning("Sin enlace con la base (%s)", type(e).__name__)
        if ok and self.ultimo is False:
            log.info("Volvió el enlace con la base")
        self.ultimo = ok
        return ok

"""La hora de la planta.

El simulador corre acelerado (×360 en desarrollo, ×1800 en la corrección) y con su propia fecha: la hora
que vale es la de los mensajes de la planta, no la de esta máquina. Se toma de las lecturas de sensores,
que llegan todo el tiempo.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone


class RelojPlanta:
    def __init__(self) -> None:
        self._ultima: datetime | None = None
        self._candado = threading.Lock()

    def observar(self, ts: datetime) -> None:
        """Registra la hora de un mensaje de la planta. Puede ir para atrás si cambia el escenario."""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        with self._candado:
            self._ultima = ts

    def ahora(self) -> datetime:
        """La hora de la planta o, mientras no llegó ningún mensaje, la de la máquina."""
        with self._candado:
            return self._ultima or datetime.now(timezone.utc)

    @property
    def sincronizado(self) -> bool:
        with self._candado:
            return self._ultima is not None

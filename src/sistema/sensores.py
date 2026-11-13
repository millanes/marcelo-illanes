"""Desvíos sostenidos de los sensores de la planta (planta/sensores/#), en código y sin modelos.

Una lectura fuera de los límites de alarma de /api/planta no alcanza: tiene que seguir afuera durante
SOSTENIDO_MIN minutos de planta (la hora de las lecturas, no la de la máquina). Así un pico aislado no escala.
Mientras dura el episodio no se repite el evento; cuando el valor vuelve adentro de los límites por VUELTA_MIN
minutos, el episodio se cierra y un desvío nuevo genera otro evento.

Una bomba parada o disparada sigue mostrando presión de descarga (la de succión, cerca de 1 kg/cm2): eso no
es un desvío de presión, es una bomba que no está en marcha, y lo dicen el parte y el estado de la planta.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from planta_contrato import EventoSistema, LecturaSensor, TipoEvento

from .equipos import FuenteDatosPlanta

log = logging.getLogger(__name__)

SOSTENIDO_MIN = 10
VUELTA_MIN = 5
PARADA = 0.2   # presión de descarga por debajo del 20 % de la alarma baja: la bomba no está en marcha

NOMBRE = {"presion_descarga": "presión de descarga", "presion_succion": "presión de succión",
          "temperatura_aceite": "temperatura de aceite", "vibracion": "vibración",
          "presion_diferencial": "presión diferencial", "caudal": "caudal"}


@dataclass
class _Episodio:
    desde: datetime | None = None          # primera lectura afuera de esta racha
    dentro_desde: datetime | None = None   # desde cuándo volvió adentro
    publicado: bool = False
    extremo: float | None = None


class DetectorSensores:
    def __init__(self, fuente: FuenteDatosPlanta, publicar: Callable[[EventoSistema], object]) -> None:
        self._fuente = fuente
        self._publicar = publicar
        self._episodios: dict[tuple[str, str], _Episodio] = {}
        self._ultima: datetime | None = None

    def lectura(self, l: LecturaSensor) -> EventoSistema | None:
        """Se llama por cada lectura (desde el hilo de red de MQTT: no puede trabarse)."""
        if self._ultima and l.ts < self._ultima:   # cambió el escenario: la hora volvió para atrás
            self._episodios.clear()
        self._ultima = l.ts
        datos = self._fuente.cargados   # sin pedir nada por HTTP: si todavía no están, se espera
        limite = datos.limites.get((l.tag, l.variable)) if datos else None
        if limite is None or l.valor is None or l.calidad.value != "buena":
            return None
        alta, baja = limite.alarma_alta, limite.alarma_baja
        parada = l.variable == "presion_descarga" and baja is not None and l.valor < PARADA * baja
        fuera = not parada and ((alta is not None and l.valor >= alta) or (baja is not None and l.valor <= baja))
        clave = (l.tag, l.variable)
        ep = self._episodios.setdefault(clave, _Episodio())
        if not fuera:
            if ep.desde is not None:
                ep.dentro_desde = ep.dentro_desde or l.ts
                if l.ts - ep.dentro_desde >= timedelta(minutes=VUELTA_MIN):
                    self._episodios[clave] = _Episodio()
            return None
        ep.dentro_desde = None
        ep.desde = ep.desde or l.ts
        arriba = alta is not None and l.valor >= alta
        ep.extremo = l.valor if ep.extremo is None else (max if arriba else min)(ep.extremo, l.valor)
        if ep.publicado or l.ts - ep.desde < timedelta(minutes=SOSTENIDO_MIN):
            return None
        ep.publicado = True
        minutos = int((l.ts - ep.desde).total_seconds() // 60)
        cual = f"sobre la alarma alta ({alta:g})" if arriba else f"bajo la alarma baja ({baja:g})"
        evento = EventoSistema(
            id=f"EV-SENSOR-{l.tag}-{l.variable}-{ep.desde:%Y%m%dT%H%M}", ts=l.ts, tipo=TipoEvento.escalado,
            equipo=l.tag, origen="sensor",
            resumen=f"{l.tag}: {NOMBRE.get(l.variable, l.variable)} {cual} hace {minutos} min: ahora {l.valor:g} "
                    f"{l.unidad}, extremo {ep.extremo:g} {l.unidad}")
        log.info("Desvío sostenido: %s", evento.resumen)
        self._publicar(evento)
        return evento

"""Las notas de voz de la recorrida.

Cada nota pasa por un carril rápido, una por vez y en orden de llegada: bajar el audio, transcribirlo y
buscar condiciones inseguras. Si hay una, la alerta sale enseguida (aviso en la tablet y evento
AlertaSeguridad): el corrector da 30 s reales desde que la planta publica la nota. Después se guarda la
nota y se le pasa a los pasos siguientes (el borrador del parte).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from planta_contrato import EventoSistema, MensajeTablet, NotaRecorrida, Pantalla, TipoEvento, Topicos
from pydantic import BaseModel

from .equipos import Catalogo, FuenteDatosPlanta
from .persistencia import Almacen
from .planta import ClientePlanta
from .reloj import RelojPlanta
from .seguridad import Hallazgo, detectar
from .transcripcion import Transcriptor, segundos

log = logging.getLogger(__name__)


@dataclass
class NotaProcesada:
    nota: NotaRecorrida
    texto: str
    hallazgos: list[Hallazgo]
    segundos_audio: float
    tiempos: dict[str, float] = field(default_factory=dict)   # segundos desde que llegó la nota


class ProcesadorNotas:
    def __init__(self, planta: ClientePlanta, crear_transcriptor: Callable[[], Transcriptor], bus,
                 reloj: RelojPlanta, almacen: Almacen | None = None, fuente: FuenteDatosPlanta | None = None) -> None:
        self._planta = planta
        self._crear_transcriptor = crear_transcriptor
        self._transcriptor: Transcriptor | None = None
        self._bus = bus
        self._reloj = reloj
        self._almacen = almacen
        self._fuente = fuente or FuenteDatosPlanta(planta.planta)
        self.siguientes: list[Callable[[NotaProcesada], None]] = []
        self._cola: queue.Queue[tuple[NotaRecorrida, float] | None] = queue.Queue()
        self._hilo = threading.Thread(target=self._atender, name="notas", daemon=True)

    # ------------------------------------------------------------------ ciclo de vida

    def arrancar(self) -> None:
        self._hilo.start()

    def detener(self) -> None:
        self._cola.put(None)

    def recibir(self, nota: NotaRecorrida) -> None:
        """Manejador de planta/recorridas: encola y vuelve enseguida."""
        self._cola.put((nota, time.monotonic()))

    def preparar(self) -> None:
        """Carga los datos de la planta y el modelo de transcripción antes de la primera nota."""
        self._fuente.obtener()
        self.transcriptor

    @property
    def transcriptor(self) -> Transcriptor:
        if self._transcriptor is None:
            self._transcriptor = self._crear_transcriptor()
        return self._transcriptor

    @property
    def catalogo(self) -> Catalogo | None:
        """Sin catálogo las alertas salen igual, sin equipo; se reintenta leerlo en la próxima nota."""
        datos = self._fuente.obtener()
        return datos.catalogo if datos else None

    def _atender(self) -> None:
        try:
            self.preparar()
        except Exception:
            log.exception("No se pudo preparar el procesador de notas")
        while (item := self._cola.get()) is not None:
            nota, llegada = item
            try:
                self.procesar(nota, llegada)
            except Exception:
                log.exception("No se pudo procesar la nota %s", nota.id)

    # ------------------------------------------------------------------ carril rápido

    def procesar(self, nota: NotaRecorrida, llegada: float | None = None) -> NotaProcesada | None:
        llegada = time.monotonic() if llegada is None else llegada
        if not nota.audio_url:
            log.warning("La nota %s no trae audio: no hay nada que transcribir", nota.id)
            return None
        tiempos: dict[str, float] = {"espera": time.monotonic() - llegada}
        audio = self._planta.descargar(nota.audio_url)
        tiempos["bajada"] = time.monotonic() - llegada
        texto = self.transcriptor.transcribir(audio)
        tiempos["transcripcion"] = time.monotonic() - llegada
        hallazgos = detectar(texto)
        if hallazgos:
            self._alertar(nota, texto, hallazgos)
            tiempos["alerta"] = time.monotonic() - llegada
        resultado = NotaProcesada(nota, texto, hallazgos, segundos(audio), tiempos)
        log.info("%s (%.1f s de audio, transcripta a los %.1f s)%s: %s", nota.id, resultado.segundos_audio,
                 tiempos["transcripcion"],
                 f" ALERTA {'+'.join(h.codigo for h in hallazgos)} a los {tiempos['alerta']:.1f} s" if hallazgos else "",
                 texto)
        if self._almacen:
            self._almacen.guardar_nota(nota, texto, [h.codigo for h in hallazgos], resultado.segundos_audio, tiempos)
        for siguiente in self.siguientes:
            siguiente(resultado)
        return resultado

    def _alertar(self, nota: NotaRecorrida, texto: str, hallazgos: list[Hallazgo]) -> None:
        catalogo = self.catalogo
        equipo = (catalogo.unico(hallazgos[0].oracion) or catalogo.unico(texto)) if catalogo else None
        codigos = "+".join(h.codigo for h in hallazgos)
        que = " y ".join(h.descripcion for h in hallazgos)
        donde = f" en {catalogo.nombre(equipo)} ({equipo})" if catalogo and equipo else ""
        aviso = MensajeTablet(
            id=f"TAB-{nota.id}-SEG", ts=self._reloj.ahora(), nota_ids=[nota.id],
            texto=f"Atención: {que}{donde}. Aplicá el procedimiento de emergencia y avisá por radio "
                  f"al supervisor de turno.",
            pantalla=Pantalla(titulo=f"Condición insegura ({codigos})"), espera_respuesta=False)
        evento = EventoSistema(
            id=f"EV-{nota.id}-SEG", ts=self._reloj.ahora(), tipo=TipoEvento.alerta_seguridad, equipo=equipo,
            origen="parte", nota_ids=[nota.id], resumen=f"{codigos}: {que}{donde}. Nota: «{texto[:300]}»")
        # Primero la tablet, que es donde está el operador; después el evento.
        self._publicar(Topicos.TABLET, aviso)
        self._publicar(Topicos.EVENTOS, evento)

    def _publicar(self, topico: str, mensaje: BaseModel, intentos: int = 5) -> bool:
        """Una alerta no se puede perder por un corte corto del broker: reintenta durante unos segundos."""
        for intento in range(intentos):
            if self._bus.publicar(topico, mensaje):
                if self._almacen:
                    self._almacen.guardar("salida", topico, mensaje.model_dump(mode="json"))
                return True
            time.sleep(1.0)
        log.error("No se pudo publicar en %s después de %d intentos", topico, intentos)
        return False

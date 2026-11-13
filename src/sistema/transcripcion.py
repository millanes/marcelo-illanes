"""Transcripción local de las notas de voz.

Provisorio: Whisper con faster-whisper (CTranslate2 en int8, CPU) hasta tener la lista cerrada de modelos
del curso. Cambiarlo es cambiar STT_MODELO. El modelo viene adentro de la imagen: en la planta no hay
internet garantizado. La planta manda WAV PCM de 16 kHz mono, que se decodifica con la biblioteca
estándar y numpy, sin ffmpeg.
"""

from __future__ import annotations

import io
import logging
import time
import wave
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

TASA = 16000

# Vocabulario de la planta: le muestra a Whisper cómo se escriben los nombres y unidades que va a escuchar.
PROMPT = ("Parte de recorrida de una planta de inyección de agua: la uno, la dos, la tres, la de la punta, "
          "la del medio, la del fondo, el filtro A, el filtro B, el pulmón, diferencial, descarga, kilos, psi, "
          "bar, empaquetadura, rodamiento, correas.")


class Transcriptor(Protocol):
    def transcribir(self, audio: bytes) -> str: ...


def muestras_wav(datos: bytes) -> np.ndarray:
    """WAV PCM de 16 bits a float32 mono de 16 kHz, que es lo que espera Whisper."""
    with wave.open(io.BytesIO(datos)) as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"WAV de {8 * w.getsampwidth()} bits: se esperaba PCM de 16 bits")
        canales, tasa = w.getnchannels(), w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if canales > 1:
        x = x.reshape(-1, canales).mean(axis=1)
    if tasa != TASA and len(x):
        n = int(round(len(x) * TASA / tasa))
        x = np.interp(np.linspace(0, len(x), n, endpoint=False), np.arange(len(x)), x).astype(np.float32)
    return x


def segundos(datos: bytes) -> float:
    with wave.open(io.BytesIO(datos)) as w:
        return w.getnframes() / float(w.getframerate())


class TranscriptorWhisper:
    def __init__(self, modelo: str, hilos: int, beam: int = 1, prompt: str | None = PROMPT) -> None:
        from faster_whisper import WhisperModel   # pesado: se importa solo si se usa

        t0 = time.perf_counter()
        # cpu_threads explícito: por defecto usa todos los núcleos de la máquina, no el límite del contenedor.
        self._modelo = WhisperModel(modelo, device="cpu", compute_type="int8", cpu_threads=hilos)
        self._beam, self._prompt = beam, prompt
        log.info("Modelo de transcripción %s cargado en %.1f s (%d hilos)", modelo, time.perf_counter() - t0, hilos)

    def transcribir(self, audio: bytes) -> str:
        segmentos, _ = self._modelo.transcribe(muestras_wav(audio), language="es", beam_size=self._beam,
                                               vad_filter=True, initial_prompt=self._prompt,
                                               condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segmentos).strip()

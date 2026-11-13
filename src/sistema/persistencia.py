"""Postgres de la planta, con pgvector.

Guarda lo que entra de la planta y lo que el sistema publica, cada nota con su transcripción y el estado de
la conversación de cada parte con el operador (borrador, intento, último mensaje), para retomarla si el
sistema se reinicia. Más adelante, los manuales indexados.

La base puede no estar disponible (arranque, corte de luz): el resto del sistema sigue andando y la
conexión se reintenta, como mucho cada REINTENTO_S segundos.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Literal

import psycopg
from planta_contrato import NotaRecorrida
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

REINTENTO_S = 5.0

ESQUEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS mensajes (
    id          bigserial PRIMARY KEY,
    registrado  timestamptz NOT NULL DEFAULT now(),
    direccion   text NOT NULL CHECK (direccion IN ('entrada', 'salida')),
    topico      text NOT NULL,
    payload     jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS mensajes_por_topico ON mensajes (topico, registrado);

-- Cada nota de voz: lo que se transcribió, si había una condición insegura y cuánto tardó cada etapa.
CREATE TABLE IF NOT EXISTS notas (
    id              text PRIMARY KEY,
    ts              timestamptz NOT NULL,
    operador        text NOT NULL,
    recorrida_id    text NOT NULL,
    audio_url       text,
    fotos           jsonb NOT NULL DEFAULT '[]',
    transcripcion   text,
    seguridad       text[] NOT NULL DEFAULT '{}',
    segundos_audio  real,
    tiempos         jsonb NOT NULL DEFAULT '{}',
    procesada       timestamptz NOT NULL DEFAULT now()
);

-- Cada llamado a un modelo de lenguaje: tokens y tiempo, para el registro de consumo y costo.
CREATE TABLE IF NOT EXISTS usos_modelo (
    id              bigserial PRIMARY KEY,
    registrado      timestamptz NOT NULL DEFAULT now(),
    modelo          text NOT NULL,
    tarea           text NOT NULL,
    tokens_entrada  int NOT NULL,
    tokens_salida   int NOT NULL,
    segundos        real NOT NULL,
    ok              boolean NOT NULL,
    error           text
);

-- La conversación de cada parte con el operador: lo que hace falta para retomarla después de un reinicio.
CREATE TABLE IF NOT EXISTS partes (
    nota_id     text PRIMARY KEY,
    nota        jsonb NOT NULL,
    texto       text NOT NULL,
    estado      text NOT NULL,
    intento     int NOT NULL DEFAULT 0,
    preguntas   int NOT NULL DEFAULT 0,
    mensaje_id  text,
    filas       jsonb NOT NULL DEFAULT '[]',
    seguridad   text[] NOT NULL DEFAULT '{}',
    actualizado timestamptz NOT NULL DEFAULT now()
);
"""


class Almacen:
    def __init__(self, url: str) -> None:
        self.url = url
        self._conexion: psycopg.Connection | None = None
        self._candado = threading.Lock()
        self._no_antes_de = 0.0   # para no reintentar la conexión en cada mensaje mientras la base no está

    def preparar(self) -> bool:
        """Conecta y crea el esquema si hace falta. Se llama al arrancar, para no esperar al primer mensaje."""
        with self._candado:
            return self._conectar() is not None

    def guardar(self, direccion: Literal["entrada", "salida"], topico: str, payload: Any) -> bool:
        with self._candado:
            conexion = self._conectar()
            if conexion is None:
                return False
            try:
                conexion.execute("INSERT INTO mensajes (direccion, topico, payload) VALUES (%s, %s, %s)",
                                 (direccion, topico, Jsonb(payload)))
                return True
            except psycopg.Error as e:
                log.warning("No se pudo guardar en Postgres: %s", e)
                self._descartar()
                return False

    def guardar_nota(self, nota: NotaRecorrida, transcripcion: str, seguridad: list[str], segundos_audio: float,
                     tiempos: dict[str, float]) -> bool:
        """Guarda (o actualiza, si ya estaba) una nota con su transcripción."""
        with self._candado:
            conexion = self._conectar()
            if conexion is None:
                return False
            try:
                conexion.execute(
                    """INSERT INTO notas (id, ts, operador, recorrida_id, audio_url, fotos, transcripcion, seguridad,
                                          segundos_audio, tiempos)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET transcripcion = EXCLUDED.transcripcion,
                           seguridad = EXCLUDED.seguridad, segundos_audio = EXCLUDED.segundos_audio,
                           tiempos = EXCLUDED.tiempos, procesada = now()""",
                    (nota.id, nota.ts, nota.operador, nota.recorrida_id, nota.audio_url, Jsonb(nota.fotos),
                     transcripcion, seguridad, segundos_audio, Jsonb({k: round(v, 3) for k, v in tiempos.items()})))
                return True
            except psycopg.Error as e:
                log.warning("No se pudo guardar la nota %s: %s", nota.id, e)
                self._descartar()
                return False

    def guardar_parte(self, nota: NotaRecorrida, texto: str, estado: str, intento: int, preguntas: int,
                      mensaje_id: str | None, filas: list[dict], seguridad: list[str]) -> bool:
        with self._candado:
            conexion = self._conectar()
            if conexion is None:
                return False
            try:
                conexion.execute(
                    """INSERT INTO partes (nota_id, nota, texto, estado, intento, preguntas, mensaje_id, filas, seguridad)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (nota_id) DO UPDATE SET estado = EXCLUDED.estado, intento = EXCLUDED.intento,
                           preguntas = EXCLUDED.preguntas, mensaje_id = EXCLUDED.mensaje_id,
                           filas = EXCLUDED.filas, actualizado = now()""",
                    (nota.id, Jsonb(nota.model_dump(mode="json")), texto, estado, intento, preguntas, mensaje_id,
                     Jsonb(filas), seguridad))
                return True
            except psycopg.Error as e:
                log.warning("No se pudo guardar el parte de %s: %s", nota.id, e)
                self._descartar()
                return False

    def guardar_uso(self, uso) -> bool:
        """Un llamado a un modelo de lenguaje (sistema.llm.Uso)."""
        with self._candado:
            conexion = self._conectar()
            if conexion is None:
                return False
            try:
                conexion.execute(
                    """INSERT INTO usos_modelo (modelo, tarea, tokens_entrada, tokens_salida, segundos, ok, error)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (uso.modelo, uso.tarea, uso.tokens_entrada, uso.tokens_salida, uso.segundos, uso.ok, uso.error))
                return True
            except psycopg.Error as e:
                log.warning("No se pudo guardar el uso del modelo: %s", e)
                self._descartar()
                return False

    def partes_abiertas(self) -> list[dict]:
        """Las conversaciones que esperaban una respuesta del operador."""
        with self._candado:
            conexion = self._conectar()
            if conexion is None:
                return []
            try:
                cursor = conexion.execute(
                    """SELECT nota, texto, estado, intento, preguntas, mensaje_id, filas, seguridad
                       FROM partes WHERE estado IN ('pregunta', 'borrador') ORDER BY actualizado""")
                columnas = [c.name for c in cursor.description]
                return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]
            except psycopg.Error as e:
                log.warning("No se pudieron leer los partes abiertos: %s", e)
                self._descartar()
                return []

    def _conectar(self) -> psycopg.Connection | None:
        if self._conexion is not None and not self._conexion.closed:
            return self._conexion
        if time.monotonic() < self._no_antes_de:
            return None
        try:
            self._conexion = psycopg.connect(self.url, autocommit=True, connect_timeout=3)
            self._conexion.execute(ESQUEMA)
            log.info("Conectado a Postgres")
            return self._conexion
        except psycopg.Error as e:
            log.warning("Postgres no está disponible: %s", e)
            self._descartar()
            return None

    def _descartar(self) -> None:
        if self._conexion is not None:
            try:
                self._conexion.close()
            except psycopg.Error:
                pass
        self._conexion = None
        self._no_antes_de = time.monotonic() + REINTENTO_S

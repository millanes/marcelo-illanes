"""Los manuales en Postgres con pgvector: el índice que se arma al arrancar y la búsqueda por similitud.

rag_fragmentos tiene una fila por fragmento (sección de un manual u orden del historial) con su cita, los
metadatos (documento, sección, título, fabricante, equipos, modelo, revisión, vigente, si aplica a la planta y
por qué, códigos de falla) y el embedding. rag_indice guarda los dos archivos que se indexaron, el modelo y una
huella de todo eso: al arrancar, si la planta sirve los mismos archivos y el modelo es el mismo, no se
re-indexa. Con los archivos guardados, el buscador arranca aunque la planta no conteste.

La búsqueda es SQL simple (ORDER BY embedding <=> consulta, con los filtros en el WHERE). Son 701 fragmentos:
la búsqueda exacta tarda milisegundos y no hace falta un índice aproximado. Si la base no está, el buscador
sigue con BM25 solo y la conexión se reintenta, como mucho cada REINTENTO_S segundos.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import date

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from ..persistencia import crear_esquema
from .busqueda import Ajustes, Buscador, Embebedor
from .corpus import PLANTA, Corpus, Fragmento, preparar

log = logging.getLogger(__name__)

REINTENTO_S = 5.0
VERSION = 1   # subirla si cambia cómo se preparan los fragmentos: obliga a re-indexar

ARCHIVO_FRAGMENTOS = "/documentos/fragmentos.jsonl"
ARCHIVO_INDICE = "/documentos/indice.json"

ESQUEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

-- Una fila por sección de manual o por orden del historial, con sus metadatos y su embedding.
CREATE TABLE IF NOT EXISTS rag_fragmentos (
    cita        text PRIMARY KEY,
    documento   text NOT NULL,
    seccion     text NOT NULL,
    titulo      text NOT NULL,
    texto       text NOT NULL,
    vigente     boolean NOT NULL,
    aplica      boolean NOT NULL,
    motivo      text,
    tipo        text NOT NULL,
    fabricante  text,
    revision    text,
    modelo      text,
    equipos     text[] NOT NULL DEFAULT '{}',
    otros       text[] NOT NULL DEFAULT '{}',
    codigos     text[] NOT NULL DEFAULT '{}',
    clave       text,
    pagina      int,
    fecha       date,
    embedding   vector NOT NULL
);
CREATE INDEX IF NOT EXISTS rag_fragmentos_por_documento ON rag_fragmentos (documento);

-- Qué se indexó: los archivos de la planta, el modelo y la huella de los dos.
CREATE TABLE IF NOT EXISTS rag_indice (
    id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    huella      text NOT NULL,
    modelo      text NOT NULL,
    dimension   int NOT NULL,
    fragmentos  int NOT NULL,
    segundos    real NOT NULL,
    archivo_fragmentos text NOT NULL,
    archivo_indice     text NOT NULL,
    indexado    timestamptz NOT NULL DEFAULT now()
);
"""

_COLUMNAS = ("cita", "documento", "seccion", "titulo", "texto", "vigente", "aplica", "motivo", "tipo", "fabricante",
             "revision", "modelo", "equipos", "otros", "codigos", "clave", "pagina", "fecha", "embedding")


def huella(fragmentos: str, indice: str, modelo: str) -> str:
    h = hashlib.sha256()
    for parte in (str(VERSION), modelo, fragmentos, indice):
        h.update(parte.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def leer_archivos(fragmentos: str, indice: str, planta: str = PLANTA) -> Corpus:
    """El corpus a partir del contenido de fragmentos.jsonl e indice.json."""
    lineas = [json.loads(linea) for linea in fragmentos.splitlines() if linea.strip()]
    return preparar(lineas, json.loads(indice), planta)


class BasePgvector:
    """La conexión a Postgres para el RAG, con el tipo vector registrado. Reintenta como Almacen
    (persistencia.py), pero es otra conexión: una indexación larga no traba el registro de mensajes."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._conexion: psycopg.Connection | None = None
        self._candado = threading.Lock()
        self._no_antes_de = 0.0

    def _conectar(self) -> psycopg.Connection | None:
        if self._conexion is not None and not self._conexion.closed:
            return self._conexion
        if time.monotonic() < self._no_antes_de:
            return None
        try:
            self._conexion = psycopg.connect(self.url, autocommit=True, connect_timeout=3)
            crear_esquema(self._conexion, ESQUEMA)
            register_vector(self._conexion)
            return self._conexion
        except psycopg.Error as e:
            log.warning("Postgres no está disponible para los manuales: %s", e)
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

    def disponible(self) -> bool:
        with self._candado:
            return self._conectar() is not None

    def estado(self) -> dict | None:
        """Lo que hay indexado (huella, modelo, archivos), o None si no hay nada o no hay base."""
        with self._candado:
            conexion = self._conectar()
            if conexion is None:
                return None
            try:
                cursor = conexion.execute(
                    """SELECT huella, modelo, dimension, fragmentos, segundos, archivo_fragmentos, archivo_indice,
                              indexado FROM rag_indice WHERE id = 1""")
                fila = cursor.fetchone()
                return dict(zip([c.name for c in cursor.description], fila)) if fila else None
            except psycopg.Error as e:
                log.warning("No se pudo leer el estado del índice de manuales: %s", e)
                self._descartar()
                return None

    def guardar(self, corpus: Corpus, matriz: np.ndarray, fragmentos: str, indice: str, modelo: str,
                segundos: float) -> bool:
        """Reemplaza todo el índice en una transacción: si se corta a la mitad, queda el anterior."""
        filas = [(f.cita, f.documento, f.numero, f.titulo, f.texto, f.vigente, f.aplica, f.motivo, f.tipo,
                  f.fabricante, f.revision, f.modelo, list(f.equipos), list(f.otros), list(f.codigos), f.clave,
                  f.pagina, date.fromisoformat(f.fecha) if f.fecha else None, matriz[i])
                 for i, f in enumerate(corpus.fragmentos)]
        with self._candado:
            conexion = self._conectar()
            if conexion is None:
                return False
            try:
                with conexion.transaction():
                    conexion.execute("DELETE FROM rag_fragmentos")
                    with conexion.cursor() as cursor:
                        cursor.executemany(
                            f"INSERT INTO rag_fragmentos ({', '.join(_COLUMNAS)}) "
                            f"VALUES ({', '.join(['%s'] * len(_COLUMNAS))})", filas)
                    conexion.execute(
                        """INSERT INTO rag_indice (id, huella, modelo, dimension, fragmentos, segundos,
                                                   archivo_fragmentos, archivo_indice)
                           VALUES (1, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (id) DO UPDATE SET huella = EXCLUDED.huella, modelo = EXCLUDED.modelo,
                               dimension = EXCLUDED.dimension, fragmentos = EXCLUDED.fragmentos,
                               segundos = EXCLUDED.segundos, archivo_fragmentos = EXCLUDED.archivo_fragmentos,
                               archivo_indice = EXCLUDED.archivo_indice, indexado = now()""",
                        (huella(fragmentos, indice, modelo), modelo, int(matriz.shape[1]), len(filas),
                         round(segundos, 1), fragmentos, indice))
                return True
            except psycopg.Error as e:
                log.warning("No se pudo guardar el índice de manuales: %s", e)
                self._descartar()
                return False


class VectoresPostgres:
    """La búsqueda por similitud en Postgres: la misma que VectoresMemoria, en SQL."""

    def __init__(self, base: BasePgvector) -> None:
        self._base = base

    def parecidos(self, vector: np.ndarray, n: int, solo_vigentes: bool) -> list[tuple[str, float]]:
        base = self._base
        with base._candado:
            conexion = base._conectar()
            if conexion is None:
                raise ConnectionError("Postgres no está disponible")
            try:
                cursor = conexion.execute(
                    """SELECT cita, 1 - (embedding <=> %(v)s) AS similitud
                       FROM rag_fragmentos
                       WHERE vigente OR NOT %(solo_vigentes)s
                       ORDER BY embedding <=> %(v)s, cita
                       LIMIT %(n)s""",
                    {"v": np.asarray(vector, dtype=np.float32), "solo_vigentes": solo_vigentes, "n": n})
                return [(cita, float(similitud)) for cita, similitud in cursor.fetchall()]
            except psycopg.Error:
                base._descartar()
                raise


class Manuales:
    """El RAG de manuales del sistema. preparar() baja los documentos de la planta, los indexa en Postgres si
    cambiaron (o si cambió el modelo) y arma el buscador; se llama en un hilo al arrancar, porque indexar los
    701 fragmentos tarda (ver evaluacion/resultados_rag.md). Hasta que está listo, buscar() devuelve []."""

    def __init__(self, descargar: Callable[[str], bytes], database_url: str, embebedor: Embebedor,
                 planta: str = PLANTA, ajustes: Ajustes | None = None) -> None:
        self._descargar = descargar      # ClientePlanta.descargar: GET a la planta con reintentos
        self.base = BasePgvector(database_url)
        self.embebedor = embebedor
        self.planta = planta
        self.ajustes = ajustes
        self.buscador: Buscador | None = None

    @property
    def listo(self) -> bool:
        return self.buscador is not None

    def preparar(self, forzar: bool = False) -> bool:
        """Deja el buscador listo. Devuelve False si no hay base (no se embebe nada que no se pueda guardar) o si
        no hay ni archivos de la planta ni un índice guardado."""
        if not self.base.disponible():
            return False
        archivos = self._bajar()
        estado = self.base.estado()
        if archivos is not None:
            fragmentos, indice = archivos
            if forzar or estado is None or estado["huella"] != huella(fragmentos, indice, self.embebedor.nombre):
                if not self.indexar(fragmentos, indice):
                    return False
            else:
                log.info("Manuales ya indexados (%d fragmentos, %s): no se re-indexa",
                         estado["fragmentos"], estado["modelo"])
        elif estado is not None and estado["modelo"] == self.embebedor.nombre:
            log.warning("La planta no sirve los documentos: se usa el índice guardado del %s", estado["indexado"])
            fragmentos, indice = estado["archivo_fragmentos"], estado["archivo_indice"]
        else:
            return False
        corpus = leer_archivos(fragmentos, indice, self.planta)
        self.buscador = Buscador(corpus, self.embebedor, VectoresPostgres(self.base), self.ajustes)
        return True

    def indexar(self, fragmentos: str, indice: str) -> bool:
        corpus = leer_archivos(fragmentos, indice, self.planta)
        inicio = time.monotonic()
        matriz = self.embebedor.documentos([f.para_embeber for f in corpus.fragmentos])
        segundos = time.monotonic() - inicio
        if not self.base.guardar(corpus, matriz, fragmentos, indice, self.embebedor.nombre, segundos):
            return False
        log.info("Manuales indexados: %d fragmentos con %s en %.0f s", len(corpus.fragmentos),
                 self.embebedor.nombre, segundos)
        return True

    def arrancar(self, parar: threading.Event, espera_s: float = 30.0) -> threading.Thread:
        """preparar() en un hilo aparte, reintentando cada espera_s hasta que salga o se pida parar.

        El hilo baja su prioridad al mínimo antes de cargar el modelo: los hilos de cálculo de ONNX que crea la
        heredan (en Linux la prioridad es por hilo), así que indexar usa la CPU que sobra y le cede todo a
        Whisper cuando llega una nota. En Windows (los tests) no hace nada."""
        def correr() -> None:
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
            except (AttributeError, OSError):
                pass
            while not parar.is_set():
                try:
                    if self.preparar():
                        return
                except Exception:
                    log.exception("No se pudieron preparar los manuales")
                parar.wait(espera_s)
        hilo = threading.Thread(target=correr, name="manuales", daemon=True)
        hilo.start()
        return hilo

    def buscar(self, consulta: str, k: int = 5, solo_vigentes: bool = True, equipo: str | None = None) -> list[dict]:
        if self.buscador is None:
            return []
        return self.buscador.buscar(consulta, k, solo_vigentes, equipo)

    def _bajar(self) -> tuple[str, str] | None:
        try:
            return (self._descargar(ARCHIVO_FRAGMENTOS).decode("utf-8"),
                    self._descargar(ARCHIVO_INDICE).decode("utf-8"))
        except Exception as e:
            log.warning("No se pudieron bajar los documentos de la planta: %s", e)
            return None

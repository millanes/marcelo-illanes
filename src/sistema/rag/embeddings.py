"""El modelo de embeddings: local, en CPU, con fastembed (ONNX Runtime). Sin GPU y con la memoria justa.

El modelo viene adentro de la imagen (ver el Dockerfile): en la planta no hay internet garantizado. Se carga la
primera vez que se usa, no al importar.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence

import numpy as np

log = logging.getLogger(__name__)

MODELO = "jinaai/jina-embeddings-v2-base-es"

# Algunos modelos esperan un prefijo distinto para la consulta y para los documentos.
PREFIJOS = {
    "google/embeddinggemma-300m": ("task: search result | query: ", "title: none | text: "),
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
}


class EmbebedorLocal:
    """Un modelo de fastembed. `lote` chico a propósito: los textos de un lote se rellenan hasta el más largo y
    la atención crece con el cuadrado del largo, así que lotes grandes de secciones largas piden mucha memoria."""

    def __init__(self, modelo: str = MODELO, cache: str | None = None, hilos: int | None = None,
                 lote: int = 8) -> None:
        self.nombre = modelo
        self.cache, self.hilos, self.lote = cache, hilos, lote
        self._modelo = None
        self._candado = threading.Lock()
        self._prefijo_consulta, self._prefijo_documento = PREFIJOS.get(modelo, ("", ""))

    def _cargar(self):
        with self._candado:
            if self._modelo is None:
                from fastembed import TextEmbedding   # import tardío: pesa y no hace falta en los tests

                inicio = time.monotonic()
                self._modelo = TextEmbedding(self.nombre, cache_dir=self.cache, threads=self.hilos)
                log.info("Modelo de embeddings %s cargado en %.1f s", self.nombre, time.monotonic() - inicio)
            return self._modelo

    def documentos(self, textos: Sequence[str]) -> np.ndarray:
        """Un vector normalizado por texto. Ordena por largo para que cada lote tenga textos parecidos."""
        modelo = self._cargar()
        orden = sorted(range(len(textos)), key=lambda i: len(textos[i]))
        vectores = list(modelo.embed([self._prefijo_documento + textos[i] for i in orden], batch_size=self.lote))
        matriz = np.zeros((len(textos), len(vectores[0]) if vectores else 0), dtype=np.float32)
        for i, v in zip(orden, vectores):
            matriz[i] = v
        return _unitarios(matriz)

    def consulta(self, texto: str) -> np.ndarray:
        modelo = self._cargar()
        return _unitarios(np.asarray(next(iter(modelo.embed([self._prefijo_consulta + texto]))),
                                     dtype=np.float32)[None, :])[0]


def _unitarios(matriz: np.ndarray) -> np.ndarray:
    normas = np.linalg.norm(matriz, axis=1, keepdims=True)
    return matriz / np.where(normas == 0, 1, normas)

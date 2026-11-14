"""Configuración del sistema. Sale de variables de entorno; por defecto, los nombres de la red de planta."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Config:
    nombre_sistema: str = "marcelo-illanes"
    mqtt_host: str = "broker"
    mqtt_port: int = 1883
    sim_url: str = "http://sim:8100"
    database_url: str = "postgresql://sistema:sistema@postgres:5432/sistema"
    latido_s: float = 30.0
    # Para saber si hay enlace se hace un pedido corto a internet a través del proxy de la planta. No se usa
    # ningún servicio de ahí: cualquier respuesta HTTP quiere decir que el radioenlace anda.
    enlace_url: str = "https://integrate.api.nvidia.com/v1/models"
    enlace_timeout_s: float = 5.0
    # Transcripción local. El modelo viene adentro de la imagen (ver Dockerfile).
    stt_modelo: str = "/modelos/whisper"
    stt_hilos: int = 2
    stt_beam: int = 1
    # Modelo de lenguaje local (Ollama). EXTRACTOR: "reglas", "llm" o "mixto" (reglas y el modelo para lo
    # que las reglas no entienden).
    llm_url: str = "http://ollama:11434"
    llm_modelo: str = "qwen2.5:1.5b"
    llm_timeout_s: float = 60.0
    extractor: str = "mixto"
    # RAG de manuales: modelo de embeddings local, adentro de la imagen (ver Dockerfile).
    rag_modelo: str = "jinaai/jina-embeddings-v2-base-es"
    rag_cache: str = "/modelos/embeddings"
    rag_hilos: int = 2

    @classmethod
    def desde_entorno(cls, entorno: Mapping[str, str] | None = None) -> Config:
        """Cada campo se pisa con la variable de entorno del mismo nombre en mayúsculas (MQTT_HOST, ...)."""
        entorno = os.environ if entorno is None else entorno
        valores = {}
        for campo in fields(cls):
            crudo = entorno.get(campo.name.upper())
            if crudo is not None:
                valores[campo.name] = type(campo.default)(crudo)
        return cls(**valores)

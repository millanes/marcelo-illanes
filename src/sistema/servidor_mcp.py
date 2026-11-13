"""Servidor MCP con las tools de solo lectura del sistema (las de herramientas.py).

    python -m sistema.servidor_mcp            HTTP streamable en http://0.0.0.0:8000/mcp
    python -m sistema.servidor_mcp --stdio    por stdio, para un cliente que lo lanza como subproceso

Adentro de otro proceso, arrancar_en_hilo(herramientas) lo levanta por HTTP en un hilo aparte. Se configura con
variables de entorno: SIM_URL (la planta, por defecto http://sim:8100), MCP_HOST y MCP_PORT (por defecto 0.0.0.0
y 8000) y MATERIALES_YAML (por defecto datos/materiales.yaml).

Cada tool se registra con su modelo de entrada tal cual: mismos campos, mismas descripciones y extra="forbid". Si
al SDK se le pasa una función suelta, arma los argumentos con su firma e ignora en silencio los que sobran; acá un
argumento de más vuelve como error. La salida va como structured content (con su output schema) y como texto JSON;
si trae `error`, el resultado se marca isError para que el cliente sepa que la tool no pudo contestar.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import create_model

from .config import Config
from .herramientas import DEFINICIONES, BuscadorManuales, ClienteConsultas, Definicion, Herramientas, Salida

log = logging.getLogger(__name__)

RUTA = "/mcp"

INSTRUCCIONES = (
    "Consultas de solo lectura sobre la planta de inyección de agua PIA-3: equipos y límites de alarma, historial de "
    "sensores, órdenes de trabajo, repuestos y stock, catálogo de tareas de mantenimiento y búsqueda en manuales. "
    "Ninguna tool cambia nada en la planta. La hora es la de la planta (UTC-3, con la fecha propia del simulador), no "
    "la de tu máquina. Los límites de alarma que valen son los de la tool equipos: la Rev. C del manual de la bomba "
    "está vencida. Si una tool no puede contestar, su salida trae `error` con el motivo y si conviene reintentar.")

# Todas son de solo lectura: no cambian nada, se pueden repetir y consultan solo la planta.
SOLO_LECTURA = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                               open_world_hint=False)


def _resultado(salida: Salida) -> CallToolResult:
    """La salida como resultado MCP: structured content, el mismo JSON como texto (sin nulos) e isError si falló."""
    return CallToolResult(content=[TextContent(type="text", text=salida.model_dump_json(exclude_none=True))],
                          structured_content=salida.model_dump(mode="json"), is_error=salida.error is not None)


def _tool(herramientas: Herramientas, definicion: Definicion) -> Tool:
    """La tool MCP de una definición: los argumentos se validan con su modelo de entrada, extra="forbid" incluido."""
    argumentos = create_model(definicion.entrada.__name__, __base__=(ArgModelBase, definicion.entrada))
    metodo = getattr(herramientas, definicion.nombre)

    def llamar(**campos: Any) -> CallToolResult:
        inicio = time.monotonic()
        salida = metodo(definicion.entrada.model_validate(campos))
        log.info("Tool %s %s: %s en %.2f s", definicion.nombre, campos,
                 salida.error.tipo if salida.error else "ok", time.monotonic() - inicio)
        return _resultado(salida)

    return Tool(fn=llamar, name=definicion.nombre, title=definicion.titulo, description=definicion.descripcion,
                parameters=argumentos.model_json_schema(by_alias=True),
                fn_metadata=FuncMetadata(arg_model=argumentos, output_model=definicion.salida),
                is_async=False, annotations=SOLO_LECTURA)


def crear_servidor(herramientas: Herramientas) -> MCPServer:
    """El servidor MCP con las seis tools."""
    return MCPServer("sistema-planta", title="Sistema de la planta PIA-3", version=_version(),
                     instructions=INSTRUCCIONES, tools=[_tool(herramientas, d) for d in DEFINICIONES])


def arrancar_en_hilo(herramientas: Herramientas, host: str | None = None,
                     puerto: int | None = None) -> threading.Thread:
    """El servidor por HTTP streamable en un hilo aparte, para correrlo adentro del sistema (python -m sistema) con
    su mismo cliente de la planta y su buscador de manuales, en lugar de otro proceso. Sin host ni puerto, los de
    MCP_HOST y MCP_PORT. Si el servidor no puede arrancar (el puerto ocupado, por ejemplo), se cae solo ese hilo."""
    por_defecto = direccion()
    host, puerto = host or por_defecto[0], puerto or por_defecto[1]
    hilo = threading.Thread(target=crear_servidor(herramientas).run, args=("streamable-http",), name="mcp",
                            kwargs={"host": host, "port": puerto, "streamable_http_path": RUTA}, daemon=True)
    hilo.start()
    log.info("Servidor MCP en http://%s:%d%s", host, puerto, RUTA)
    return hilo


def direccion(entorno: Mapping[str, str] | None = None) -> tuple[str, int]:
    """Host y puerto del HTTP streamable: MCP_HOST y MCP_PORT, por defecto 0.0.0.0 y 8000."""
    entorno = os.environ if entorno is None else entorno
    return entorno.get("MCP_HOST", "0.0.0.0"), int(entorno.get("MCP_PORT", "8000"))


def _version() -> str:
    try:
        return version("sistema-planta")
    except PackageNotFoundError:
        return "0"


def _buscador_rag() -> BuscadorManuales | None:
    """El buscador de manuales del RAG, si ya está: rag.crear_buscador(). Sin él, buscar_manuales contesta con un
    error sin_buscador y las otras cinco tools andan igual."""
    try:
        from . import rag
    except ImportError:
        log.warning("Todavía no hay sistema.rag: buscar_manuales va a contestar sin_buscador")
        return None
    except Exception:
        log.exception("No se pudo importar sistema.rag: buscar_manuales va a contestar sin_buscador")
        return None
    crear = getattr(rag, "crear_buscador", None)
    if crear is None:
        log.warning("sistema.rag no tiene crear_buscador(): buscar_manuales va a contestar sin_buscador")
        return None
    try:
        return crear()
    except Exception:
        log.exception("No se pudo crear el buscador de manuales: buscar_manuales va a contestar sin_buscador")
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m sistema.servidor_mcp",
                                     description="Servidor MCP con las tools de solo lectura del sistema.")
    parser.add_argument("--stdio", action="store_true", help="atender por stdio en lugar de HTTP streamable")
    args = parser.parse_args(argv)
    # Los logs van a stderr: con --stdio, stdout es el canal del protocolo.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = Config.desde_entorno()
    servidor = crear_servidor(Herramientas(ClienteConsultas(cfg.sim_url), buscador=_buscador_rag()))
    if args.stdio:
        log.info("Servidor MCP por stdio, planta %s", cfg.sim_url)
        servidor.run("stdio")
    else:
        host, puerto = direccion()
        log.info("Servidor MCP en http://%s:%d%s, planta %s", host, puerto, RUTA, cfg.sim_url)
        servidor.run("streamable-http", host=host, port=puerto, streamable_http_path=RUTA)


if __name__ == "__main__":
    main()

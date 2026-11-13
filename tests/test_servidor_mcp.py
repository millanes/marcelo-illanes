import json

import anyio
import pytest
from mcp import Client
from planta_falsa import BuscadorFalso, PlantaFalsa, herramientas

from sistema.herramientas import DEFINICIONES
from sistema.servidor_mcp import crear_servidor, direccion


def con_cliente(planta: PlantaFalsa, pedir, buscador=None, modo: str = "auto"):
    """Levanta el servidor en memoria, sin red, y corre `pedir(cliente)` con un cliente MCP conectado."""
    async def correr():
        async with Client(crear_servidor(herramientas(planta, buscador)), mode=modo) as cliente:
            return await pedir(cliente)
    return anyio.run(correr)


def test_lista_las_seis_tools_de_solo_lectura():
    tools = con_cliente(PlantaFalsa(), lambda c: c.list_tools()).tools

    assert [t.name for t in tools] == [d.nombre for d in DEFINICIONES]
    for tool, definicion in zip(tools, DEFINICIONES):
        assert tool.description == definicion.descripcion and tool.title == definicion.titulo
        assert tool.input_schema["additionalProperties"] is False       # extra="forbid" también por MCP
        assert set(tool.input_schema["properties"]) == set(definicion.entrada.model_fields)
        assert tool.output_schema["title"] == definicion.salida.__name__
        assert tool.annotations.read_only_hint and not tool.annotations.destructive_hint
    sensores = tools[1].input_schema["properties"]
    assert sensores["horas"]["maximum"] == 168 and "hora de la planta" in sensores["hasta"]["description"]


@pytest.mark.parametrize("modo", ["auto", "legacy"])
def test_llama_una_tool_de_punta_a_punta(modo):
    planta = PlantaFalsa()

    resultado = con_cliente(planta, lambda c: c.call_tool("equipos", {"nombre": "la del medio"}), modo=modo)

    assert not resultado.is_error
    assert [e["tag"] for e in resultado.structured_content["equipos"]] == ["B-102"]
    assert json.loads(resultado.content[0].text)["equipos"][0]["unidad_manometro"] == "psi"
    assert planta.rutas() == ["/api/planta"]


def test_historial_de_sensores_por_mcp():
    planta = PlantaFalsa()
    argumentos = {"tag": "B-101", "variable": "vibracion", "hasta": "2026-11-12T14:00:00-03:00", "horas": 2,
                  "tramos": 4}

    resultado = con_cliente(planta, lambda c: c.call_tool("historial_sensores", argumentos))

    assert not resultado.is_error
    assert resultado.structured_content["resumen"]["lecturas"] == 120
    assert len(resultado.structured_content["tramos"]) == 4
    assert planta.pedidos[-1].url.params["desde"] == "2026-11-12T12:00:00-03:00"


def test_un_argumento_de_mas_es_error_y_no_llega_a_la_planta():
    planta = PlantaFalsa()

    resultado = con_cliente(planta, lambda c: c.call_tool(
        "historial_sensores", {"tag": "B-101", "variable": "vibracion", "dias": 3}))

    assert resultado.is_error
    assert "dias" in resultado.content[0].text and "extra_forbidden" in resultado.content[0].text
    assert planta.pedidos == []


def test_un_error_de_la_planta_vuelve_estructurado_y_con_is_error():
    planta = PlantaFalsa()
    planta.fallar("/api/planta", 503, 503, 503)

    resultado = con_cliente(planta, lambda c: c.call_tool("equipos", {}))

    assert resultado.is_error
    error = resultado.structured_content["error"]
    assert (error["tipo"], error["reintentable"], error["estado_http"]) == ("error_planta", True, 503)
    assert json.loads(resultado.content[0].text)["error"]["tipo"] == "error_planta"
    assert len(planta.pedidos) == 3


def test_buscar_manuales_por_mcp_con_y_sin_buscador():
    sin = con_cliente(PlantaFalsa(), lambda c: c.call_tool("buscar_manuales", {"consulta": "cartuchos"}))
    con = con_cliente(PlantaFalsa(), lambda c: c.call_tool("buscar_manuales", {"consulta": "cartuchos"}),
                      buscador=BuscadorFalso())

    assert sin.is_error and sin.structured_content["error"]["tipo"] == "sin_buscador"
    assert not con.is_error and con.structured_content["fragmentos"][0]["cita"] == "TX350-D §3.2"


def test_host_y_puerto_salen_del_entorno():
    assert direccion({}) == ("0.0.0.0", 8000)
    assert direccion({"MCP_HOST": "127.0.0.1", "MCP_PORT": "8765"}) == ("127.0.0.1", 8765)

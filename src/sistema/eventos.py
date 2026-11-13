"""Detección en planta: de un parte a eventos SinNovedad o Escalado, en código y sin modelos.

Un equipo se escala si el parte dice algo anormal de él (cualquier observación que no sea "parada, en
reserva" o "parada por mantenimiento") o si una lectura pasa un límite de alarma de /api/planta. Con los
mismos datos la decisión da siempre lo mismo, y el id del evento sale de la nota y el equipo: publicarlo dos
veces es el mismo evento.
"""

from __future__ import annotations

from datetime import datetime

from planta_contrato import EventoSistema, TipoEvento

from .equipos import A_KG, Limite
from .extractor import Fila

NORMALES = {"parada, en reserva", "parada por mantenimiento"}
# Lecturas de campo que tienen un sensor con límites de alarma.
SENSOR_DE = {"presion_descarga_local": "presion_descarga", "presion_diferencial": "presion_diferencial"}
SIN_EQUIPO = {None, "?", "PLANTA"}


def motivos(filas: list[Fila], limites: dict[tuple[str, str], Limite]) -> dict[str, list[str]]:
    """Para cada equipo del parte, por qué escalarlo. Una lista vacía es sin novedad."""
    salida: dict[str, list[str]] = {}
    for f in filas:
        if f.tag in SIN_EQUIPO or f.variable == "condicion_insegura":
            continue
        lista = salida.setdefault(f.tag, [])
        if f.observacion and f.observacion not in NORMALES:
            lista.append(f.observacion)
        sensor = SENSOR_DE.get(f.variable)
        limite = limites.get((f.tag, sensor)) if sensor else None
        if limite and f.valor is not None:
            kg = f.valor * A_KG.get(f.unidad or "kg/cm2", 1.0)
            if limite.alarma_alta is not None and kg >= limite.alarma_alta:
                lista.append(f"{f.variable} {f.valor:g} {f.unidad} sobre la alarma alta ({limite.alarma_alta:g} kg/cm2)")
            elif limite.alarma_baja is not None and kg <= limite.alarma_baja:
                lista.append(f"{f.variable} {f.valor:g} {f.unidad} bajo la alarma baja ({limite.alarma_baja:g} kg/cm2)")
    return salida


def eventos_del_parte(nota_id: str, filas: list[Fila], limites: dict[tuple[str, str], Limite], ts: datetime,
                      contexto: dict[str, str] | None = None) -> list[EventoSistema]:
    """Un evento por equipo del parte; si el parte no nombra ningún equipo, uno sin novedad para la nota.
    `contexto` agrega, por equipo, lo que acompaña a un escalado (fotos, tendencia de las últimas horas)."""
    por_equipo = motivos(filas, limites)
    parte_id = f"PARTE-{nota_id}"
    if not por_equipo:
        return [EventoSistema(id=f"EV-{nota_id}-SN", ts=ts, tipo=TipoEvento.sin_novedad, origen="parte",
                              parte_id=parte_id, nota_ids=[nota_id], resumen="Recorrida sin novedad")]
    salida = []
    for tag, lista in por_equipo.items():
        if lista:
            resumen = f"{tag}: " + "; ".join(lista)
            if contexto and contexto.get(tag):
                resumen += f". {contexto[tag]}"
            tipo = TipoEvento.escalado
        else:
            resumen, tipo = f"{tag}: sin novedad", TipoEvento.sin_novedad
        salida.append(EventoSistema(id=f"EV-{nota_id}-{tag}", ts=ts, tipo=tipo, equipo=tag, origen="parte",
                                    parte_id=parte_id, nota_ids=[nota_id], resumen=resumen))
    return salida

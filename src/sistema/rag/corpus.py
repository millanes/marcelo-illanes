"""El corpus de documentos de la planta, preparado para buscar.

Cada línea de fragmentos.jsonl (una sección de un manual o una orden del historial) pasa a ser un Fragmento
con su cita y los metadatos que hacen falta para no caer en las trampas del corpus:

- vigente: la Rev. C del manual de la bomba está reemplazada (lo dicen indice.json y la línea misma);
- aplica / motivo / otros: secciones de otros modelos (TX-250, TX-500, FC-24, FC-96), de otras plantas
  (PIA-1, PIA-2), opcionales que la planta no tiene e instalación o preservación. Se detectan por el título
  ("Límites de otras plantas"), por lo que declara el texto ("Estos valores no aplican a la TX-350") y por
  el capítulo, que arrastra a sus secciones ("17. Secciones de otras plantas");
- codigos: los fabricantes usan sus códigos de falla (TX-T07, FC-T02) y la planta los suyos (F01 a F10). La
  correspondencia se lee del propio corpus (la tabla del MBP-4 §6.1), no está escrita acá;
- equipos: los TAG a los que aplica cada documento (indice.json) y, en las órdenes, el equipo de la orden y
  los que nombra el texto ("CAVITACION EN B2"), con los nombres de campo del MBP-4 §2.2 y §4.4;
- periodo: los años que cubre una lista de precios ("Precios vigentes 2025-2026"; "Solo para certificar
  trabajos iniciados antes del 1 de enero de 2025"), para que un trabajo de 2024 use la lista vieja.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..equipos import Catalogo, Equipo
from ..texto import normalizar

PLANTA = "PIA-3"

_MODELO = re.compile(r"\b([A-Z]{2})-(\d{2,3})\b(?!-)")        # TX-350, FC-48; no TX-T07, TX-VAL-KIT ni FC-48-AR
_PLANTA = re.compile(r"\bPIA-\d+\b")
_TAG = re.compile(r"^[A-Z]{1,2}-\d{3}[A-Z]?$")                # B-101, F-201A, TK-301
_FALLA_PLANTA = re.compile(r"\bF\d{2}\b")                     # F01 a F10
_FALLA_FABRICANTE = re.compile(r"\b[A-Z]{2}-T\d{2}\b")        # TX-T07, FC-T02
# "Estos valores no aplican a la TX-350", "No aplican a PIA-3", "No aplica a la operación", "No aplica a bombas
# en servicio", "No forma parte de la operación".
_NO_APLICA = re.compile(r"\bno aplica(?:n)? a (?:la |el |los |las )?(?P<a>[^.]+)"
                        r"|\bno forma parte de la operaci[oó]n", re.IGNORECASE)
_DECLARA_OPERACION = re.compile(r"operaci[oó]n|en servicio|en reserva", re.IGNORECASE)
# "Precios vigentes 2025-2026"; "Solo para certificar trabajos iniciados antes del 1 de enero de 2025".
_PERIODO_TITULO = re.compile(r"\b(20\d{2})\s*[-–]\s*(20\d{2})\b")
_SOLO_ANTES = re.compile(r"\bsolo para [^.]*\bantes del \d{1,2} de \w+ de (20\d{2})", re.IGNORECASE)


@dataclass(frozen=True)
class Fragmento:
    cita: str                       # "TX350-D §3.2", "historial OT-25-0026": la cita de la suite y de la planta
    documento: str                  # "TX350-D", "historial"
    numero: str                     # la sección ("3.2") o la orden ("OT-25-0026")
    titulo: str
    texto: str
    vigente: bool = True
    clave: str | None = None
    pagina: int | None = None
    tipo: str = ""                  # manual_fabricante, manual_operadora, contrato, historial
    fabricante: str | None = None   # el emisor del documento ("Trimax Industrial S.A.")
    revision: str | None = None
    modelo: str | None = None       # el modelo de equipo del manual ("TX-350")
    equipos: tuple[str, ...] = ()   # TAG a los que aplica; vacío: a toda la planta
    aplica: bool = True             # False: es de otro modelo, de otra planta, un opcional, instalación...
    motivo: str | None = None       # por qué no aplica a la planta
    otros: tuple[str, ...] = ()     # otros modelos o plantas que nombra ("TX-250", "PIA-1")
    codigos: tuple[str, ...] = ()   # códigos de falla de la sección: el de la planta y los del fabricante
    fecha: str | None = None        # la de la orden, en el historial
    periodo: tuple[int, int] | None = None   # los años que cubre (listas de precios)
    encabezado: str = ""            # el documento, solo para BM25 ("Bomba ... TX-350, Rev. D")
    agregado: str = ""              # lo que se suma al indexar: correspondencia de códigos, nombres del equipo

    @property
    def para_bm25(self) -> str:
        """El texto que se indexa por palabras: el documento, la sección y lo agregado."""
        return "\n".join(p for p in (self.encabezado, self.texto, self.agregado) if p)

    @property
    def para_embeber(self) -> str:
        """El texto que se embebe: la sección y lo agregado, sin el documento. Un encabezado común a todas las
        secciones de un manual las acerca entre sí y el modelo distingue peor (ver evaluacion/resultados_rag.md)."""
        return "\n".join(p for p in (self.texto, self.agregado) if p)

    def como_dict(self, puntaje: float | None = None) -> dict:
        salida = {"cita": self.cita, "documento": self.documento, "seccion": self.numero, "titulo": self.titulo,
                  "texto": self.texto, "vigente": self.vigente, "puntaje": puntaje, "pagina": self.pagina,
                  "clave": self.clave, "tipo": self.tipo, "fabricante": self.fabricante, "revision": self.revision,
                  "modelo": self.modelo, "equipos": list(self.equipos), "aplica": self.aplica,
                  "motivo": self.motivo, "otros": list(self.otros), "codigos": list(self.codigos),
                  "fecha": self.fecha, "periodo": list(self.periodo) if self.periodo else None}
        if puntaje is None:
            del salida["puntaje"]
        return salida


@dataclass(frozen=True)
class Falla:
    codigo: str                     # "F09"
    nombre: str                     # "Cavitación por baja succión"
    fabricante: tuple[str, ...]     # ("TX-T07",)


@dataclass
class Corpus:
    fragmentos: list[Fragmento]
    fallas: dict[str, Falla] = field(default_factory=dict)          # por código de la planta
    de_fabricante: dict[str, str] = field(default_factory=dict)     # "TX-T07" -> "F09"
    nombres_fabricante: dict[str, str] = field(default_factory=dict)  # "TX-T07" -> "Cavitación por succión insuficiente"
    catalogo: Catalogo | None = None
    modelos: frozenset[str] = frozenset()     # los de la planta: TX-350, FC-48
    planta: str = PLANTA

    def traducir(self, codigo: str) -> list[str]:
        """Los códigos equivalentes: "TX-T07" -> ["F09"]; "F10" -> ["TX-T08", "FC-T04"]."""
        codigo = codigo.upper()
        if codigo in self.fallas:
            return list(self.fallas[codigo].fabricante)
        return [self.de_fabricante[codigo]] if codigo in self.de_fabricante else []

    def describir(self, codigo: str) -> str:
        """"TX-T07" -> "F09 Cavitación por baja succión (TX-T07 Cavitación por succión insuficiente)"."""
        codigo = codigo.upper()
        f = self.fallas.get(codigo) or self.fallas.get(self.de_fabricante.get(codigo, ""))
        if f is None:
            return codigo
        de_fabrica = ", ".join(f"{c} {self.nombres_fabricante.get(c, '')}".strip() for c in f.fabricante)
        return f"{f.codigo} {f.nombre} ({de_fabrica})"

    def otros_nombrados(self, texto: str) -> set[str]:
        """Los modelos de las familias de la planta y las plantas que nombra un texto y no son de esta planta."""
        familias = {m.split("-")[0] for m in self.modelos}
        hallados = {m.group(0) for m in _MODELO.finditer(texto) if m.group(1) in familias}
        hallados |= set(_PLANTA.findall(texto))
        return {h for h in hallados if h not in self.modelos and h != self.planta}


def _celdas(linea: str) -> list[str]:
    return [c.strip() for c in linea.split("|")]


def leer_fallas(lineas: Iterable[dict]) -> tuple[dict[str, Falla], dict[str, str]]:
    """La correspondencia del catálogo de fallas (MBP-4 §6.1): "F09 | Cavitación por baja succión | TX-T07 | ..."."""
    fallas: dict[str, Falla] = {}
    nombres: dict[str, str] = {}
    for linea in lineas:
        for fila in linea.get("texto", "").splitlines():
            celdas = _celdas(fila)
            if len(celdas) < 3:
                continue
            if re.fullmatch(r"F\d{2}", celdas[0]) and (codigos := _FALLA_FABRICANTE.findall(celdas[2])):
                fallas.setdefault(celdas[0], Falla(celdas[0], celdas[1], tuple(codigos)))
        # Los títulos de las secciones de falla del fabricante: "TX-T07 Cavitación por succión insuficiente".
        if m := re.match(r"([A-Z]{2}-T\d{2}) (.+)", linea.get("titulo") or ""):
            nombres.setdefault(m.group(1), m.group(2))
    return fallas, nombres


def leer_equipos(lineas: Iterable[dict]) -> Catalogo | None:
    """Los nombres de campo de cada equipo, de las tablas de nombres y abreviaturas del corpus (MBP-4 §2.2:
    "B-101 | Bomba alternativa triplex 1 | la uno, la 1, ..."; §4.4: "B1, Bba 1 | B-101")."""
    nombres: dict[str, list[str]] = {}
    for linea in lineas:
        filas = [f for f in linea.get("texto", "").splitlines() if "|" in f]
        # Solo las columnas de nombres o abreviaturas: la descripción ("Bomba alternativa triplex 1") no es un nombre.
        columnas = [i for i, c in enumerate(_celdas(filas[0]))
                    if re.search(r"\b(nombres?|abreviaturas?)\b", c, re.IGNORECASE)] if filas else []
        if not columnas:
            continue
        for fila in filas[1:]:
            celdas = _celdas(fila)
            tags = [c for c in celdas if _TAG.match(c)]
            if len(tags) != 1:
                continue
            for i in columnas:
                if i < len(celdas):
                    nombres.setdefault(tags[0], []).extend(n.strip() for n in celdas[i].split(",") if n.strip())
    if not nombres:
        return None
    return Catalogo([Equipo(tag=t, tipo="", nombres=tuple(n)) for t, n in nombres.items()])


def _motivo(titulo: str, texto: str, otros: set[str], corpus: Corpus) -> str | None:
    """Por qué una sección no aplica a la planta, o None si aplica."""
    t = normalizar(titulo)
    en_titulo = {o for o in otros if o in titulo}
    if en_titulo or "otros modelos" in t or "otras plantas" in t:
        return _de_quien(en_titulo or otros, corpus, "otras plantas" in t)
    if t == "opcionales":
        return "opcional: hay que ver en la placa si el equipo lo tiene"
    for m in _NO_APLICA.finditer(texto):
        destinatario = m.group("a") or ""
        if any(x in destinatario for x in (*corpus.modelos, corpus.planta)):
            return _de_quien(otros, corpus, corpus.planta in destinatario)
        if not destinatario or _DECLARA_OPERACION.search(destinatario):
            return "no es de la operación: instalación, preservación o transporte"
        if "trabajos nuevos" in destinatario:
            return "solo para trabajos anteriores"
    return None


def _de_quien(otros: set[str], corpus: Corpus, de_planta: bool) -> str:
    plantas = sorted(o for o in otros if _PLANTA.fullmatch(o))
    if plantas or de_planta:
        cuales = f" ({', '.join(plantas)})" if plantas else ""
        return f"de otra planta{cuales}, no de {corpus.planta}"
    modelos = sorted(otros)
    return f"de otro modelo ({', '.join(modelos)})" if modelos else "de otro modelo"


def _periodo(titulo: str, texto: str) -> tuple[int, int] | None:
    """Los años que cubre una sección, si los declara: "Precios vigentes 2025-2026" o "Solo para certificar
    trabajos iniciados antes del 1 de enero de 2025" (hasta 2024)."""
    if m := _PERIODO_TITULO.search(titulo):
        return int(m.group(1)), int(m.group(2))
    if m := _SOLO_ANTES.search(texto):
        return 1900, int(m.group(1)) - 1
    return None


def _capitulo(numero: str) -> str | None:
    """El capítulo de una sección numerada ("17.1" -> "17"), o None si ya es capítulo o es un anexo (los anexos
    juntan cosas sueltas y no arrastran nada a sus secciones)."""
    if "." not in numero:
        return None
    capitulo = numero.split(".")[0]
    return capitulo if capitulo.isdigit() else None


def _encabezado(doc: dict) -> str:
    if doc.get("tipo") == "historial":
        return ""
    revision = f", Rev. {doc['revision']}" if doc.get("revision") else ""
    vencido = " (reemplazada)" if doc.get("vigente") is False else ""
    return f"{doc.get('titulo', doc.get('id', ''))}{revision}{vencido}"


def preparar(lineas: list[dict], indice: dict, planta: str = PLANTA) -> Corpus:
    """Arma el corpus con las líneas de fragmentos.jsonl y los metadatos de indice.json."""
    docs = {d["id"]: d for d in indice.get("documentos", [])}
    modelos = {m.group(0) for d in docs.values() if d.get("tipo") == "manual_fabricante"
               for m in _MODELO.finditer(d.get("titulo", ""))}
    fallas, nombres_fabricante = leer_fallas(lineas)
    corpus = Corpus([], fallas, {c: f.codigo for f in fallas.values() for c in f.fabricante}, nombres_fabricante,
                    leer_equipos(lineas), frozenset(modelos), planta)

    # Lo que declara cada capítulo vale para sus secciones ("17. Secciones de otras plantas").
    por_capitulo: dict[tuple[str, str], tuple[str | None, set[str]]] = {}
    for linea in lineas:
        if linea["doc"] != "historial" and "." not in str(linea["numero"]):
            titulo, texto = linea.get("titulo") or "", linea.get("texto") or ""
            otros = corpus.otros_nombrados(titulo + "\n" + texto)
            por_capitulo[(linea["doc"], str(linea["numero"]))] = (_motivo(titulo, texto, otros, corpus), otros)

    for linea in lineas:
        doc = docs.get(linea["doc"], {})
        numero = str(linea["numero"])
        texto, titulo = linea.get("texto") or "", linea.get("titulo") or ""
        if linea["doc"] == "historial":
            corpus.fragmentos.append(_orden(linea, corpus))
            continue
        otros = corpus.otros_nombrados(titulo + "\n" + texto)
        motivo = _motivo(titulo, texto, otros, corpus)
        if motivo is None and (capitulo := _capitulo(numero)):
            motivo_capitulo, otros_capitulo = por_capitulo.get((linea["doc"], capitulo), (None, set()))
            if motivo_capitulo:
                motivo, otros = motivo_capitulo, otros | otros_capitulo
        modelo = next((m.group(0) for m in _MODELO.finditer(doc.get("titulo", "")) if m.group(0) in modelos), None)
        codigos, agregado = _codigos(titulo, linea.get("clave"), corpus)
        corpus.fragmentos.append(Fragmento(
            cita=linea["id"], documento=linea["doc"], numero=numero, titulo=titulo, texto=texto,
            vigente=bool(linea.get("vigente", True)) and doc.get("vigente", True) is not False,
            clave=linea.get("clave"), pagina=linea.get("pagina"), tipo=doc.get("tipo", ""),
            fabricante=doc.get("emisor"), revision=doc.get("revision"), modelo=modelo,
            equipos=tuple(doc.get("equipos", [])), aplica=motivo is None, motivo=motivo, otros=tuple(sorted(otros)),
            codigos=codigos, periodo=_periodo(titulo, texto), encabezado=_encabezado(doc), agregado=agregado))
    return corpus


def _codigos(titulo: str, clave: str | None, corpus: Corpus) -> tuple[tuple[str, ...], str]:
    """Los códigos de falla de una sección de falla ("TX-T07 Cavitación..." o "F09 Cavitación...") y la línea que
    se agrega al indexar con la equivalencia, para que "F09" encuentre la sección del fabricante y al revés."""
    propios = _FALLA_FABRICANTE.findall(titulo) or _FALLA_PLANTA.findall(titulo[:4])
    if not propios and clave:
        propios = _FALLA_FABRICANTE.findall(clave) or _FALLA_PLANTA.findall(clave)
    codigos: list[str] = []
    for c in propios:
        codigos += [c, *corpus.traducir(c)]
    codigos = list(dict.fromkeys(codigos))
    if not codigos or not corpus.traducir(propios[0]):
        return tuple(codigos), ""
    return tuple(codigos), f"Código de falla: {corpus.describir(propios[0])}."


_ORDEN = re.compile(r"^(?P<numero>\S+) · (?P<fecha>\d{4}-\d{2}-\d{2}) · (?P<equipo>\S+) · (?P<tarea>[^·]+?) · ")


def _orden(linea: dict, corpus: Corpus) -> Fragmento:
    """Una orden del historial: "OT-25-0119 · 2025-12-02 · B-101 · T-B-ALI · Servipat\\nAviso: ...\\nTrabajo: ..."."""
    texto = linea.get("texto") or ""
    m = _ORDEN.match(texto)
    propio = m.group("equipo") if m else None
    equipos = [propio] if propio and _TAG.match(propio) else []
    if corpus.catalogo is not None:
        equipos += [x.tag for x in corpus.catalogo.menciones(texto)]
    equipos = list(dict.fromkeys(equipos))
    agregado = ""
    if corpus.catalogo is not None and equipos:
        # Los nombres de campo del equipo: la orden dice "B3" o "la del fondo" y la consulta "la tres".
        agregado = "Equipo: " + "; ".join(
            f"{t} ({', '.join(corpus.catalogo.equipos[t].nombres)})"
            for t in equipos if t in corpus.catalogo.equipos)
    # Sin encabezado: uno común a las 243 órdenes ("historial de PIA-3") las acerca a cualquier consulta que nombre
    # la planta.
    return Fragmento(
        cita=linea["id"], documento="historial", numero=str(linea["numero"]), titulo=linea.get("titulo") or "",
        texto=texto, vigente=bool(linea.get("vigente", True)), tipo="historial",
        equipos=tuple(equipos), fecha=m.group("fecha") if m else None, agregado=agregado)

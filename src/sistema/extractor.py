"""Del texto de una nota a las filas del parte, con reglas.

Busca anclas ("descarga", "diferencial", "metros", "grados", "empaquetadura", "aceite", "correas",
"golpeteo", "parada"...). Para cada una toma el equipo nombrado antes (en la misma frase o el último que se
nombró) y, si la variable es numérica, el número dicho justo antes con su unidad. No depende de la
puntuación, que Whisper pone donde quiere ("El de la derecha, medio, doce, de diferencial").

Marca lo que hay que preguntar: "la bomba" sin decir cuál, y un diferencial dicho "dieciocho" en vez de
"uno ocho". Es determinista y tarda microsegundos.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from planta_contrato import FilaParte

from .equipos import Catalogo
from .numeros import CONECTORES, es_numero, leer, preparar
from .texto import normalizar

AMBIGUA = "?"   # tag provisorio de "la bomba" a secas
UNIDADES_DICHAS = {
    "kilos": "kg/cm2", "kilo": "kg/cm2", "kg": "kg/cm2", "kgs": "kg/cm2", "kilogramos": "kg/cm2",
    "kilogramo": "kg/cm2", "psi": "psi", "libras": "psi", "libra": "psi", "lb": "psi", "lbs": "psi",
    "bar": "bar", "bares": "bar", "metros": "m", "metro": "m", "mts": "m", "grados": "degC", "grado": "degC",
}
RELLENO = {"de", "en", "por", "y"}   # entre el número y el ancla: "112 kg de descarga", "1640 psi y de descarga"
NUMERICAS = {"presion_descarga_local", "presion_diferencial", "nivel", "temperatura_rodamiento_ir",
             "temperatura_motor_ir"}
UNIDAD_FIJA = {"presion_diferencial": "kg/cm2", "nivel": "m", "temperatura_rodamiento_ir": "degC",
               "temperatura_motor_ir": "degC"}
TIPO_DE = {"presion_descarga_local": "bomba", "temperatura_rodamiento_ir": "bomba", "temperatura_motor_ir": "bomba",
           "goteo_empaquetadura": "bomba", "nivel_aceite_visor": "bomba", "estado_correas": "bomba",
           "ruido": "bomba", "estado": "bomba", "presion_diferencial": "filtro", "nivel": "tanque"}
DIFERENCIAL_MAXIMO = 5.0     # un diferencial de 5 o más es un "dieciocho" que era "uno ocho"
RODAMIENTO_CALIENTE = 75.0
DIFERENCIAL_ALTO = 1.5

_ANCLAS = {
    "descarga": "presion_descarga_local", "diferencial": "presion_diferencial",
    "metros": "nivel", "metro": "nivel", "mts": "nivel", "grados": "temperatura", "grado": "temperatura",
    "empaquetadura": "goteo_empaquetadura", "aceite": "nivel_aceite_visor", "correas": "estado_correas",
    "correa": "estado_correas", "golpeteo": "ruido", "metalico": "ruido", "piedras": "ruido", "ruido": "ruido",
    "parada": "estado", "parado": "estado", "disparada": "estado", "disparado": "estado", "arranca": "estado",
    "rebalsando": "rebalse", "rebalse": "rebalse", "rebalsa": "rebalse", "foto": "foto", "fotos": "foto",
}
_LA_BOMBA = re.compile(r"(?<![a-z0-9ñ])la bomba(?! (?:uno|dos|tres|1|2|3)\b)(?![a-z0-9ñ])")


@dataclass
class Fila:
    tag: str | None
    variable: str
    valor: float | None = None
    unidad: str | None = None
    observacion: str | None = None
    foto: str | None = None
    ambigua: str | None = None   # "equipo" o "valor": hay que preguntarle al operador
    dicho: str | None = None     # cómo se dijo lo ambiguo ("la bomba", "dieciocho")

    def contrato(self) -> FilaParte:
        return FilaParte(tag=self.tag or AMBIGUA, variable=self.variable, valor=self.valor, unidad=self.unidad,
                         observacion=self.observacion)

    def como_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Mencion:
    inicio: int    # índice del primer token
    fin: int       # índice del token siguiente al nombre
    tag: str
    dicho: str


def _me_dio(tokens: list[str]) -> list[str]:
    """Whisper escribe "medio" donde el operador dijo "me dio": si le sigue un número, es "me dio"."""
    salida: list[str] = []
    for i, t in enumerate(tokens):
        siguiente = tokens[i + 1] if i + 1 < len(tokens) else ""
        if t == "medio" and es_numero(siguiente):
            salida += ["me", "dio"]
        else:
            salida.append(t)
    return salida


def _menciones(unido: str, catalogo: Catalogo) -> list[_Mencion]:
    def token(c: int) -> int:
        return unido[:c].count(" ")

    salida = [_Mencion(token(m.inicio), token(m.fin) + 1, m.tag, m.dicho) for m in catalogo.menciones(unido)]
    salida += [_Mencion(token(m.start()), token(m.end()) + 1, AMBIGUA, "la bomba") for m in _LA_BOMBA.finditer(unido)]
    return sorted(salida, key=lambda m: m.inicio)


def _eco_de_unidad(tokens: list[str], j: int) -> bool:
    """Whisper a veces repite el final de la unidad: "1610 psi, si de descarga"."""
    return j > 0 and tokens[j - 1] in UNIDADES_DICHAS and len(tokens[j]) >= 2 and tokens[j - 1].endswith(tokens[j]) \
        and tokens[j - 1] != tokens[j]


def _numero_antes(tokens: list[str], i: int, limite: int) -> tuple[float | None, str | None, str]:
    """El número dicho justo antes del ancla en la posición i, su unidad y cómo se dijo."""
    j, unidad = i - 1, None
    while j >= limite and (tokens[j] in RELLENO or tokens[j] in UNIDADES_DICHAS or _eco_de_unidad(tokens, j)):
        unidad = unidad or UNIDADES_DICHAS.get(tokens[j])
        j -= 1
    fin = j + 1
    while j >= limite and (es_numero(tokens[j]) or tokens[j] in CONECTORES):
        j -= 1
    tramo = tokens[j + 1:fin]
    while tramo and tramo[0] in CONECTORES:   # "y", "con", "medio" sueltos adelante no son del número
        tramo = tramo[1:]
    return (leer(tramo) if tramo else None), unidad, " ".join(tramo)


def _observacion(variable: str, tokens: list[str], i: int) -> str | None:
    antes, despues = tokens[max(0, i - 5):i], tokens[i + 1:i + 5]
    if variable == "goteo_empaquetadura":
        if {"chorrea", "chorreando"} & set(antes) or ("pierde" in antes and "bastante" in antes):
            return "pérdida importante por empaquetadura"
        if {"gotea", "goteando", "goteo"} & set(antes):
            return "goteo por empaquetadura"
        return "pérdida por empaquetadura" if {"pierde", "perdida"} & set(antes) else None
    if variable == "nivel_aceite_visor":
        return "nivel de aceite bajo" if "bajo" in despues or "casi" in antes or "falta" in antes else None
    if variable == "estado_correas":
        if "estan" in despues:
            k = despues.index("estan")
            if k + 1 < len(despues):
                return f"correas {despues[k + 1]}"
        return "correas con problemas" if {"chillan", "flojas", "gastadas"} & set(despues) else None
    if variable == "ruido":
        t = tokens[i]
        if t == "golpeteo" or t.endswith("metalico"):
            return "hace un golpeteo metálico"
        if t == "piedras":
            return "suena como si tuviera piedras adentro"
        return "tiene un ruido raro de rodamiento" if "rodamiento" in despues else "hace un ruido raro"
    if variable == "estado":
        t = tokens[i]
        if t in ("disparada", "disparado") or (t == "arranca" and "no" in antes[-2:]):
            return "disparada, no arranca"
        if t in ("parada", "parado"):
            return "parada por mantenimiento" if "mantenimiento" in despues else "parada, en reserva"
    return None


def extraer(texto: str, catalogo: Catalogo, fotos: list[str] | None = None) -> list[Fila]:
    """Las filas del parte que dice el texto, en el orden en que se dijeron."""
    tokens = _me_dio(normalizar(preparar(texto)).split())
    unido = " ".join(tokens)
    menciones = _menciones(unido, catalogo)
    tanque = next(iter(catalogo.de_tipo("tanque")), None)
    filas: list[Fila] = []
    vistas: set[tuple[str | None, str]] = set()
    fotos_libres = list(fotos or [])

    for i, t in enumerate(tokens):
        # Whisper a veces pega o parte las palabras: "golpe teometálico" sigue siendo un golpeteo metálico.
        ancla = _ANCLAS.get(t) or ("ruido" if t.endswith("metalico") else None)
        if ancla is None:
            continue
        previas = [m for m in menciones if m.fin <= i]
        mencion = previas[-1] if previas else None
        if ancla == "foto":
            sin_foto = [f for f in filas if f.foto is None]
            if sin_foto and fotos_libres:
                sin_foto[-1].foto = fotos_libres.pop(0)
            continue
        if ancla == "rebalse":
            nivel = next((f for f in filas if f.variable == "nivel"), None)
            if nivel:
                nivel.observacion = "rebalse a pileta"
            elif tanque and (tanque, "nivel") not in vistas:
                filas.append(Fila(tanque, "nivel", unidad="m", observacion="rebalse a pileta"))
                vistas.add((tanque, "nivel"))
            continue
        variable = ancla
        if ancla == "temperatura":
            variable = "temperatura_motor_ir" if "motor" in tokens[max(0, i - 6):i] else "temperatura_rodamiento_ir"
        tag = mencion.tag if mencion else None
        if variable == "nivel" and (tag is None or catalogo.equipos.get(tag, None) is None
                                    or catalogo.equipos[tag].tipo != "tanque"):
            tag = tanque
        # Si el último equipo nombrado no es del tipo que corresponde ("el pulmón ... me dio 1,2 de
        # diferencial"), el equipo de esta lectura no está claro.
        if tag not in (None, AMBIGUA) and catalogo.equipos[tag].tipo != TIPO_DE.get(variable, catalogo.equipos[tag].tipo):
            tag = None
        if (tag, variable) in vistas:
            continue

        fila = Fila(tag, variable)
        if variable in NUMERICAS:
            valor, unidad, dicho = _numero_antes(tokens, i, mencion.fin if mencion else 0)
            if valor is None:
                continue   # "se rompió algo en la descarga": la palabra está, la lectura no
            fila.valor = valor
            fila.unidad = UNIDAD_FIJA.get(variable) or unidad or (
                catalogo.equipos[tag].unidad_manometro if tag not in (None, AMBIGUA) else None)
            if variable == "presion_diferencial" and valor >= DIFERENCIAL_MAXIMO:
                fila.valor, fila.ambigua, fila.dicho = round(valor / 10, 1), "valor", dicho
            if variable == "presion_diferencial" and fila.valor >= DIFERENCIAL_ALTO:
                fila.observacion = "diferencial alto"
            if variable == "temperatura_rodamiento_ir" and valor > RODAMIENTO_CALIENTE:
                fila.observacion = "rodamiento caliente"
        else:
            fila.observacion = _observacion(variable, tokens, i)
            if fila.observacion is None:
                continue
        if tag in (None, AMBIGUA):
            fila.tag, fila.ambigua, fila.dicho = None, "equipo", (mencion.dicho if mencion else None)
        filas.append(fila)
        vistas.add((tag, variable))
    return filas

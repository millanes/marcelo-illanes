"""La búsqueda: por significado (embeddings) y por palabras (BM25), combinadas, con las trampas del corpus
resueltas en el ranking.

1. La consulta se expande: un código de falla del fabricante suma su equivalente del catálogo de la planta
   y al revés ("TX-T07" busca también "F09 Cavitación por baja succión"), con la tabla del MBP-4 §6.
2. Se buscan los N más parecidos por embeddings (en memoria con numpy o en Postgres con pgvector, ver
   postgres.py: la lógica es la misma) y los N mejores por BM25. Los filtros (solo vigentes, equipo) se
   aplican en los dos.
3. Los puntajes se llevan a 0..1 dentro de los candidatos y se suman con pesos (Ajustes.peso_vectores).
4. Ajustes por metadatos: lo que no aplica a la planta (otro modelo, otra planta, opcional, instalación)
   baja, salvo que la consulta lo nombre; lo que solo nombra otros modelos baja un poco; la revisión
   reemplazada, si se pidió, va abajo de la vigente; una lista de precios que no cubre el año nombrado
   baja; las órdenes del equipo que nombra la consulta ("la dos", "B3") y las órdenes en general, si se
   pregunta por lo que pasó ("otra vez", "¿alguna vez...?"), suben.

Si no hay modelo o no hay base, la búsqueda sigue con BM25 solo y los mismos ajustes.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .corpus import Corpus, Fragmento

log = logging.getLogger(__name__)

_PARADA = frozenset("""a al algo ante como con cual cuales cuando cuanto cuantos cuanta cuantas de del donde el
ella en es esta este esto estan hay la las le les lo los mas me mi muy no o otra otras otro otros para pero
por porque que se si sin sobre su sus un una uno unos unas y ya hace hacer tiene tienen son fue ser puede
hasta desde entre tambien cada""".split())
_PALABRA = re.compile(r"[a-z0-9ñ]+(?:-[a-z0-9ñ]+)*")
_CODIGO = re.compile(r"\b(?:[A-Z]{2}-T\d{2}|F\d{2})\b", re.IGNORECASE)
_REVISION = re.compile(r"\brev(?:\.|isi[oó]n)?\s*([A-Z0-9])\b", re.IGNORECASE)
_ANIO = re.compile(r"\b(?:19|20)\d{2}\b")
# Preguntas por lo que pasó antes: "¿qué fue las otras veces?", "otra vez", "¿alguna vez se...?", "¿cuándo se cambió?".
_PASADO = re.compile(r"\b(?:alguna vez|otra vez|otras veces|veces anteriores|vez anterior|[uú]ltima vez"
                     r"|historial|cu[aá]ndo se)\b", re.IGNORECASE)
# Lo que en campo se dice de una forma y en los manuales de otra.
_SINONIMOS = (("µm", " micrones "), ("μm", " micrones "))


def _sin_tildes(texto: str) -> str:
    t = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def tokens(texto: str, largo: int = 6) -> list[str]:
    """Palabras para BM25: minúsculas, sin tildes, sin palabras vacías y cortadas a `largo` letras (una raíz
    barata: "cambiar" y "cambio" quedan iguales). Los códigos con guion van enteros y también por partes:
    "TX-T07" da "tx-t07", "tx" y "t07"."""
    for escrito, dicho in _SINONIMOS:
        texto = texto.replace(escrito, dicho)
    salida: list[str] = []
    for palabra in _PALABRA.findall(_sin_tildes(texto)):
        partes = palabra.split("-")
        if len(partes) > 1:
            salida.append(palabra)
        salida.extend(p[:largo] for p in partes if p not in _PARADA)
    return salida


class Lexico:
    """BM25 sobre el texto indexado de cada fragmento."""

    def __init__(self, textos: Sequence[str], k1: float = 1.2, b: float = 0.75, largo: int = 6) -> None:
        self.largo, self.k1, self.b = largo, k1, b
        self._docs = [Counter(tokens(t, largo)) for t in textos]
        self._largos = np.array([sum(d.values()) for d in self._docs], dtype=np.float32)
        self._promedio = float(self._largos.mean()) if len(self._docs) else 1.0
        n = len(self._docs)
        df = Counter(t for d in self._docs for t in d)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self._posting: dict[str, list[tuple[int, int]]] = {}
        for i, d in enumerate(self._docs):
            for t, tf in d.items():
                self._posting.setdefault(t, []).append((i, tf))

    def puntajes(self, consulta: str) -> np.ndarray:
        """El puntaje BM25 de cada fragmento (0 si no comparte ninguna palabra)."""
        salida = np.zeros(len(self._docs), dtype=np.float32)
        for t, veces in Counter(tokens(consulta, self.largo)).items():
            idf = self._idf.get(t)
            if idf is None:
                continue
            for i, tf in self._posting[t]:
                norma = self.k1 * (1 - self.b + self.b * self._largos[i] / self._promedio)
                salida[i] += veces * idf * tf * (self.k1 + 1) / (tf + norma)
        return salida


class Vectores(Protocol):
    """Dónde están los embeddings de los fragmentos: en memoria (numpy) o en Postgres (pgvector)."""

    def parecidos(self, vector: np.ndarray, n: int, solo_vigentes: bool) -> list[tuple[str, float]]:
        """Las citas de los n fragmentos más parecidos al vector (similitud coseno). Con solo_vigentes, sin la
        revisión reemplazada."""
        ...


class Embebedor(Protocol):
    nombre: str

    def documentos(self, textos: Sequence[str]) -> np.ndarray:
        """Un vector normalizado por texto (una fila por texto)."""
        ...

    def consulta(self, texto: str) -> np.ndarray:
        ...


class VectoresMemoria:
    """Los embeddings en una matriz de numpy, para la evaluación, los tests o si no hay base."""

    def __init__(self, fragmentos: Sequence[Fragmento], matriz: np.ndarray) -> None:
        if len(fragmentos) != len(matriz):
            raise ValueError("Hace falta un vector por fragmento")
        self._fragmentos = list(fragmentos)
        self._matriz = np.asarray(matriz, dtype=np.float32)

    def parecidos(self, vector: np.ndarray, n: int, solo_vigentes: bool) -> list[tuple[str, float]]:
        similitudes = self._matriz @ np.asarray(vector, dtype=np.float32)
        salida = []
        for i in np.argsort(-similitudes, kind="stable"):
            f = self._fragmentos[i]
            if f.vigente or not solo_vigentes:
                salida.append((f.cita, float(similitudes[i])))
                if len(salida) == n:
                    break
        return salida


@dataclass(frozen=True)
class Ajustes:
    """Los pesos del ranking. Los valores por defecto salen de la evaluación (evaluacion/resultados_rag.md): en
    toda la zona probada (peso 0,4 a 0,6, equipo y pasado 1 a 1,6) hit@5 y recall@5 se mueven una o dos preguntas.
    Los factores multiplican el puntaje combinado, que va de 0 a 1."""
    peso_vectores: float = 0.5      # el resto es BM25
    candidatos: int = 60            # cuántos trae cada búsqueda antes de combinar
    no_aplica: float = 0.5          # otro modelo, otra planta, opcional, instalación
    nombra_otros: float = 0.85      # nombra otros modelos u otras plantas sin ser sobre ellos
    reemplazada: float = 0.5        # la revisión vieja, cuando se pide con solo_vigentes=False
    equipo_nombrado: float = 1.3    # las órdenes del equipo que nombra la consulta o que se pide (`equipo`)
    equipo_documento: float = 1.1   # los manuales del equipo que se pide (`equipo`)
    fuera_de_periodo: float = 0.6   # una lista de precios que no cubre el año que nombra la consulta
    pasado: float = 1.3             # las órdenes, si la consulta pregunta por lo que pasó ("otra vez")


@dataclass
class Consulta:
    texto: str              # lo que se busca, con la expansión de códigos
    codigos: list[str]      # los códigos de falla nombrados y sus equivalentes
    otros: set[str]         # otros modelos o plantas nombrados ("TX-500", "PIA-2")
    equipos: set[str]       # los TAG nombrados ("la dos" -> B-102)
    revisiones: set[str]    # revisiones nombradas ("Rev. C")
    anios: set[int]         # años nombrados ("un trabajo de 2024")
    pasado: bool = False    # pregunta por lo que pasó antes
    equipo: str | None = None   # el equipo que se pidió aparte (buscar(..., equipo="B-102"))


class Buscador:
    def __init__(self, corpus: Corpus, embebedor: Embebedor | None, vectores: Vectores | None,
                 ajustes: Ajustes | None = None) -> None:
        self.corpus = corpus
        self.embebedor = embebedor
        self.vectores = vectores
        self.ajustes = ajustes or Ajustes()
        self._por_cita = {f.cita: f for f in corpus.fragmentos}
        self._posicion = {f.cita: i for i, f in enumerate(corpus.fragmentos)}
        self.lexico = Lexico([f.para_bm25 for f in corpus.fragmentos])
        self._candado = threading.Lock()

    @classmethod
    def en_memoria(cls, corpus: Corpus, embebedor: Embebedor | None, matriz: np.ndarray | None = None,
                   ajustes: Ajustes | None = None) -> Buscador:
        """Con los vectores en memoria. Si no se pasa la matriz, embebe los fragmentos (tarda)."""
        vectores = None
        if embebedor is not None:
            if matriz is None:
                matriz = embebedor.documentos([f.para_embeber for f in corpus.fragmentos])
            vectores = VectoresMemoria(corpus.fragmentos, matriz)
        return cls(corpus, embebedor, vectores, ajustes)

    def analizar(self, consulta: str) -> Consulta:
        """Lo que la consulta nombra: códigos de falla, otros modelos o plantas, equipos y revisiones."""
        codigos: list[str] = []
        for c in _CODIGO.findall(consulta):
            codigos += [c.upper(), *self.corpus.traducir(c)]
        codigos = list(dict.fromkeys(codigos))
        extra = [self.corpus.describir(c) for c in dict.fromkeys(c.upper() for c in _CODIGO.findall(consulta))
                 if self.corpus.traducir(c)]
        catalogo = self.corpus.catalogo
        equipos = {m.tag for m in catalogo.menciones(consulta)} if catalogo is not None else set()
        return Consulta(texto=" ".join([consulta, *extra]), codigos=codigos,
                        otros=self.corpus.otros_nombrados(consulta), equipos=equipos,
                        revisiones={r.upper() for r in _REVISION.findall(consulta)},
                        anios={int(a) for a in _ANIO.findall(consulta)}, pasado=bool(_PASADO.search(consulta)))

    def buscar(self, consulta: str, k: int = 5, solo_vigentes: bool = True, equipo: str | None = None) -> list[dict]:
        """Los k fragmentos que mejor contestan la consulta, del más al menos relevante.

        Cada uno es un dict con cita, documento, seccion, titulo, texto, vigente, puntaje y los metadatos
        (fabricante, equipos, modelo, revision, aplica, motivo, codigos...). El puntaje solo sirve para comparar
        dentro de una búsqueda. Con solo_vigentes=False también entra la revisión reemplazada, abajo de la
        vigente. Con `equipo` ("B-102") suben sus órdenes y sus manuales, pero no se descarta el resto: la causa
        puede estar en otro equipo (la B-102 cavita porque el filtro primario está colmatado)."""
        return [f.como_dict(round(p, 4)) for f, p in self.rankear(consulta, k, solo_vigentes, equipo)]

    def citas(self, consulta: str, k: int = 20, solo_vigentes: bool = True) -> list[str]:
        """Solo las citas, en orden: lo que pide corpus.evaluar.evaluar(buscar, suite)."""
        return [f.cita for f, _ in self.rankear(consulta, k, solo_vigentes)]

    def rankear(self, consulta: str, k: int = 5, solo_vigentes: bool = True,
                equipo: str | None = None) -> list[tuple[Fragmento, float]]:
        q = self.analizar(consulta)
        if equipo:
            q.equipo = equipo
            q.equipos.add(equipo)
        a = self.ajustes
        candidatos: dict[str, list[float]] = {}   # cita -> [vectores, bm25], ya normalizados

        if self.embebedor is not None and self.vectores is not None:
            try:
                with self._candado:
                    vector = self.embebedor.consulta(q.texto)
                parecidos = self.vectores.parecidos(vector, a.candidatos, solo_vigentes)
            except Exception as e:   # sin modelo o sin base: sigue con BM25 solo
                log.warning("Búsqueda por embeddings no disponible, sigue solo con BM25: %s", e)
                parecidos = []
            for cita, valor in _normalizar(parecidos):
                candidatos.setdefault(cita, [0.0, 0.0])[0] = valor
        peso_vectores = a.peso_vectores if candidatos else 0.0

        bm25 = self.lexico.puntajes(q.texto)
        lexicos = []
        for i in np.argsort(-bm25, kind="stable"):
            if bm25[i] <= 0 or len(lexicos) == a.candidatos:
                break
            f = self.corpus.fragmentos[i]
            if f.vigente or not solo_vigentes:
                lexicos.append((f.cita, float(bm25[i])))
        for cita, valor in _normalizar(lexicos, desde_cero=True):
            candidatos.setdefault(cita, [0.0, 0.0])[1] = valor

        puntajes = []
        for cita, (v, l) in candidatos.items():
            f = self._por_cita[cita]
            puntaje = peso_vectores * v + (1 - peso_vectores) * l
            puntajes.append((f, puntaje * self._factor(f, q)))
        puntajes.sort(key=lambda x: (-x[1], self._posicion[x[0].cita]))
        return puntajes[:k]

    def _factor(self, f: Fragmento, q: Consulta) -> float:
        a = self.ajustes
        factor = 1.0
        nombrados = bool(q.otros & set(f.otros))
        en_periodo = None
        if q.anios and f.periodo:
            en_periodo = any(f.periodo[0] <= anio <= f.periodo[1] for anio in q.anios)
            if not en_periodo:
                factor *= a.fuera_de_periodo
        if not f.aplica and not nombrados and not en_periodo:
            factor *= a.no_aplica
        elif f.otros and not nombrados:
            factor *= a.nombra_otros
        if not f.vigente and (f.revision or "").upper() not in q.revisiones:
            factor *= a.reemplazada
        if q.equipos and f.tipo == "historial" and q.equipos & set(f.equipos):
            factor *= a.equipo_nombrado
        elif q.equipo and f.tipo != "historial" and q.equipo in f.equipos:
            factor *= a.equipo_documento
        if q.pasado and f.tipo == "historial":
            factor *= a.pasado
        return factor


def _normalizar(pares: list[tuple[str, float]], desde_cero: bool = False) -> list[tuple[str, float]]:
    """Lleva los puntajes a 0..1 dentro de los candidatos: el mejor vale 1. Con desde_cero, el peor posible es 0
    (BM25); si no, el peor de los candidatos (la similitud coseno nunca llega a 0 entre textos del mismo tema)."""
    if not pares:
        return []
    alto = max(p for _, p in pares)
    bajo = 0.0 if desde_cero else min(p for _, p in pares)
    if alto - bajo <= 1e-9:
        return [(c, 1.0) for c, _ in pares]
    return [(c, (p - bajo) / (alto - bajo)) for c, p in pares]

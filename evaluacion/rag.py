"""Evaluación del RAG de manuales con la suite de la planta (datos/documentos/evaluacion/retrieval.json).

    .venv/Scripts/python evaluacion/rag.py
    .venv/Scripts/python evaluacion/rag.py --modelos jinaai/jina-embeddings-v2-base-es
    .venv/Scripts/python evaluacion/rag.py --database-url postgresql://postgres:x@localhost:55432/postgres

Corre el evaluador oficial (corpus.evaluar.evaluar(buscar, suite), del repo de la planta: la variable
PLANTA_REPO o ../academia2026-tp) con k = 1, 3, 5 y 10, lo compara con la línea de base BM25 y cuenta en
cuántas preguntas una sección de la Rev. C aparece antes que la misma sección (misma clave) de la Rev. D.
Deja todo en evaluacion/resultados_rag.md.

Cada modelo de embeddings se mide en un proceso aparte, para que la memoria sea la suya: carga, indexado de
los 701 fragmentos y consulta, con 2 hilos como el contenedor del sistema. Los vectores quedan en una carpeta
temporal (rag-evaluacion) y se reusan; --remedir los vuelve a calcular. Con --database-url, además, indexa en
ese Postgres y evalúa con el backend pgvector, que tiene que dar lo mismo que el de memoria.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PLANTA_REPO = Path(os.environ.get("PLANTA_REPO", RAIZ.parent / "academia2026-tp"))
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(PLANTA_REPO / "planta"))

import numpy as np  # noqa: E402
from corpus.evaluar import BM25, evaluar  # noqa: E402

from sistema.rag import Ajustes, Buscador, Corpus, EmbebedorLocal, Fragmento, leer_archivos  # noqa: E402

DOCUMENTOS = PLANTA_REPO / "datos" / "documentos"
# Modelo: tamaño de los archivos (GB) y hasta cuántos tokens de cada texto mira, según fastembed. MiniLM corta en
# 128 tokens (unas 90 palabras): de las secciones largas solo ve el principio. potion es estático (sin atención).
MODELOS = {"jinaai/jina-embeddings-v2-base-es": (0.64, 8192),
           "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": (0.22, 128),
           "google/embeddinggemma-300m": (1.24, 2048),
           "minishlab/potion-multilingual-128M": (0.51, 512)}
KS = (1, 3, 5, 10)
CACHE = Path(tempfile.gettempdir()) / "rag-evaluacion"
SALIDA = RAIZ / "evaluacion" / "resultados_rag.md"
# Lo que entra en la PC de la planta (sin GPU, 2 CPU para el sistema y Whisper): archivos del modelo y consulta.
ARCHIVOS_MAX_GB = 1.0
CONSULTA_MAX_MS = 100


# --- Medición de un modelo, en su propio proceso -----------------------------------------------------------------

def memoria_mb() -> tuple[float, float]:
    """Memoria del proceso en MB: (actual, pico)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Contadores(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        *[(f"x{i}", ctypes.c_size_t) for i in range(6)]]

        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Contadores), wintypes.DWORD]
        c = Contadores()
        c.cb = ctypes.sizeof(c)
        k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.WorkingSetSize / 2**20, c.PeakWorkingSetSize / 2**20
    import resource
    actual = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    return actual, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _archivo(modelo: str, extension: str) -> Path:
    return CACHE / f"{modelo.replace('/', '__')}.{extension}"


def medir_modelo(modelo: str, hilos: int, lote: int) -> None:
    """Indexa el corpus con el modelo y guarda la matriz, las consultas de la suite y las mediciones."""
    corpus, suite = cargar()
    antes, _ = memoria_mb()
    embebedor = EmbebedorLocal(modelo, hilos=hilos, lote=lote)
    inicio = time.perf_counter()
    embebedor.consulta("calentamiento")
    carga = time.perf_counter() - inicio
    cargado, _ = memoria_mb()
    inicio = time.perf_counter()
    matriz = embebedor.documentos([f.para_embeber for f in corpus.fragmentos])
    indexado = time.perf_counter() - inicio
    inicio = time.perf_counter()
    consultas = np.stack([embebedor.consulta(q["pregunta"]) for q in suite])
    consulta = (time.perf_counter() - inicio) / len(suite)
    _, pico = memoria_mb()
    CACHE.mkdir(parents=True, exist_ok=True)
    np.save(_archivo(modelo, "npy"), matriz)
    np.save(_archivo(modelo, "consultas.npy"), consultas)
    info = {"modelo": modelo, "dimension": int(matriz.shape[1]), "hilos": hilos, "lote": lote,
            "carga_s": carga, "indexado_s": indexado, "consulta_ms": 1000 * consulta,
            "memoria_modelo_mb": cargado - antes, "memoria_pico_mb": pico}
    _archivo(modelo, "json").write_text(json.dumps(info), encoding="utf-8")


def medicion(modelo: str, hilos: int, lote: int, remedir: bool) -> tuple[np.ndarray, dict]:
    if remedir or not _archivo(modelo, "json").exists():
        print(f"Midiendo {modelo}: indexa los 701 fragmentos con {hilos} hilos...", flush=True)
        subprocess.run([sys.executable, __file__, "--medir", modelo, "--hilos", str(hilos), "--lote", str(lote)],
                       check=True)
    return np.load(_archivo(modelo, "npy")), json.loads(_archivo(modelo, "json").read_text(encoding="utf-8"))


class ConsultasMedidas:
    """Embebedor que contesta las preguntas de la suite con los vectores ya medidos y el resto con el modelo: así
    se prueban varias combinaciones sin volver a embeber."""

    def __init__(self, modelo: str, suite: list[dict]) -> None:
        self.nombre = modelo
        consultas = np.load(_archivo(modelo, "consultas.npy"))
        self._medidas = {q["pregunta"]: consultas[i] for i, q in enumerate(suite)}
        self._modelo = EmbebedorLocal(modelo)

    def consulta(self, texto: str) -> np.ndarray:
        if texto not in self._medidas:
            self._medidas[texto] = self._modelo.consulta(texto)
        return self._medidas[texto]

    def documentos(self, textos: list[str]) -> np.ndarray:
        return self._modelo.documentos(textos)


# --- Evaluación ---------------------------------------------------------------------------------------------------

def cargar() -> tuple[Corpus, list[dict]]:
    corpus = leer_archivos((DOCUMENTOS / "fragmentos.jsonl").read_text(encoding="utf-8"),
                           (DOCUMENTOS / "indice.json").read_text(encoding="utf-8"))
    suite = json.loads((DOCUMENTOS / "evaluacion" / "retrieval.json").read_text(encoding="utf-8"))
    return corpus, suite


def rev_c_antes(citas: list[str], por_cita: dict[str, Fragmento]) -> bool:
    """Si en la lista aparece una sección de la Rev. C antes que la misma sección (misma clave) de la Rev. D."""
    vistas = set()
    for cita in citas:
        f = por_cita.get(cita)
        if f is not None and f.documento == "TX350-D":
            vistas.add(f.clave)
        elif f is not None and f.documento == "TX350-C" and f.clave not in vistas:
            return True
    return False


def medir(nombre: str, buscar, suite: list[dict], por_cita: dict[str, Fragmento]) -> dict:
    """El evaluador oficial, más las preguntas con la Rev. C antes que la D en los primeros 5 y 10."""
    r = evaluar(buscar, suite, KS)
    tops = {q["id"]: buscar(q["pregunta"], 10) for q in suite}
    r.update(nombre=nombre, rev_c5=sum(rev_c_antes(t[:5], por_cita) for t in tops.values()),
             rev_c10=sum(rev_c_antes(t, por_cita) for t in tops.values()),
             ids_rev_c5=[i for i, t in tops.items() if rev_c_antes(t[:5], por_cita)])
    t = r["total"]
    print(f"  {nombre:62} hit@5 {t['hit@5']:.3f}  recall@5 {t['recall@5']:.3f}  MRR {t['mrr']:.3f}  "
          f"distractor@5 {t['distractor@5']:.3f}  Rev. C antes que D {r['rev_c5']}", flush=True)
    return r


def suite_codigos(corpus: Corpus) -> list[dict]:
    """Preguntas armadas con la tabla del MBP-4 §6.1 (no son de la suite oficial): el código del fabricante tiene que
    traer la falla del catálogo de la planta, y el código de la planta la sección del fabricante."""
    propias: dict[str, list[str]] = {}
    for f in corpus.fragmentos:
        if f.vigente and f.codigos:
            propias.setdefault(f.codigos[0], []).append(f.cita)
    suite = []
    for codigo, falla in corpus.fallas.items():
        suite.append({"id": f"cod-{codigo}", "tipo": "codigos", "distractores": [],
                      "relevantes": [c for fab in falla.fabricante for c in propias.get(fab, [])],
                      "pregunta": f"¿Qué dice el manual del fabricante sobre la falla {codigo}?"})
        for fab in falla.fabricante:
            suite.append({"id": f"cod-{fab}", "tipo": "codigos", "distractores": [], "relevantes": propias[codigo],
                          "pregunta": f"Apareció la falla {fab}, ¿qué hay que hacer?"})
    return suite


def sin_traduccion(corpus: Corpus, matriz: np.ndarray, embebedor) -> tuple[Corpus, np.ndarray]:
    """El mismo corpus sin la tabla de códigos: sin la equivalencia agregada al indexar ni la expansión de la consulta."""
    fragmentos, matriz = list(corpus.fragmentos), matriz.copy()
    cambiar = [i for i, f in enumerate(fragmentos) if f.codigos and f.agregado]
    for i in cambiar:
        fragmentos[i] = replace(fragmentos[i], agregado="")
    if cambiar:
        matriz[cambiar] = embebedor.documentos([fragmentos[i].para_embeber for i in cambiar])
    return replace(corpus, fragmentos=fragmentos, fallas={}, de_fabricante={}), matriz


def evaluar_pgvector(url: str, corpus: Corpus, matriz: np.ndarray, embebedor, en_memoria: Buscador,
                     suite: list[dict], por_cita: dict[str, Fragmento]) -> dict:
    """Guarda los vectores ya calculados en Postgres y evalúa con el backend pgvector."""
    from sistema.rag import BasePgvector, VectoresPostgres

    base = BasePgvector(url)
    inicio = time.perf_counter()
    if not base.guardar(corpus, matriz, (DOCUMENTOS / "fragmentos.jsonl").read_text(encoding="utf-8"),
                        (DOCUMENTOS / "indice.json").read_text(encoding="utf-8"), embebedor.nombre, 0.0):
        raise SystemExit(f"No se pudo indexar en {url}")
    guardar = time.perf_counter() - inicio
    buscador = Buscador(corpus, embebedor, VectoresPostgres(base), en_memoria.ajustes)
    r = medir("pgvector: híbrido + metadatos", lambda q, k: buscador.citas(q, k), suite, por_cita)
    inicio = time.perf_counter()
    iguales = sum(buscador.citas(q["pregunta"], 10) == en_memoria.citas(q["pregunta"], 10) for q in suite)
    consulta = (time.perf_counter() - inicio) / len(suite)
    return {"r": r, "iguales": iguales, "guardar_s": guardar, "consulta_ms": 1000 * consulta}


# --- Informe ------------------------------------------------------------------------------------------------------

def _gb(modelo: str) -> float:
    return MODELOS.get(modelo, (0.0, 0))[0]


def _n(x: float, dec: int = 2) -> str:
    return f"{x:.{dec}f}".replace(".", ",")


def _tabla(filas: list[dict]) -> list[str]:
    salida = ["| Configuración | hit@1 | hit@3 | hit@5 | hit@10 | recall@1 | recall@3 | recall@5 | recall@10 | MRR | "
              "distractor@5 | Rev. C antes que D (top 5 / 10) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in filas:
        t = r["total"]
        salida.append(f"| {r['nombre']} | " + " | ".join(_n(t[m]) for m in (
            "hit@1", "hit@3", "hit@5", "hit@10", "recall@1", "recall@3", "recall@5", "recall@10", "mrr",
            "distractor@5")) + f" | {r['rev_c5']} / {r['rev_c10']} |")
    return salida


def _segundos(x: float) -> str:
    return _n(x, 0 if x >= 10 else 1)


def informe(a: dict) -> str:
    base, elegido, info = a["base"], a["elegido"], a["elegido_info"]
    b, e = base["total"], elegido["total"]
    lexico, con, sin = a["lexico"]["total"], *a["codigos"]
    corto = info["modelo"].split("/")[-1]
    solo = next(m["vectores"]["total"] for m in a["modelos"] if m["info"]["modelo"] == info["modelo"])
    filas_modelos = []
    for m in a["modelos"]:
        i, v, h = m["info"], m["vectores"]["total"], m["hibrido"]["total"]
        filas_modelos.append(
            f"| `{i['modelo']}` | {i['dimension']} | {_n(_gb(i['modelo']))} GB | {MODELOS[i['modelo']][1]} | "
            f"{_n(v['hit@5'])} / {_n(v['recall@5'])} | {_n(h['hit@5'])} / {_n(h['recall@5'])} / {_n(h['mrr'])} | "
            f"{_segundos(i['carga_s'])} s | {_segundos(i['indexado_s'])} s | {_segundos(i['consulta_ms'])} ms | "
            f"{i['memoria_modelo_mb']:.0f} / {i['memoria_pico_mb']:.0f} MB |")
    tipos = elegido["por_tipo"]
    por_tipo = [f"| {tipo} | {m['n']} | {_n(m['recall@5'])} | {_n(tipos[tipo]['recall@5'])} | {_n(m['hit@5'])} | "
                f"{_n(tipos[tipo]['hit@5'])} |" for tipo, m in base["por_tipo"].items()]
    mejora = max(base["por_tipo"], key=lambda t: tipos[t]["recall@5"] - base["por_tipo"][t]["recall@5"])
    con_c, crudo = a["con_c"], a["con_c_sin_ajustes"]
    lineas = [
        "# RAG de manuales: resultados",
        "",
        f"Generado por `evaluacion/rag.py` el {time.strftime('%d/%m/%Y')} con el evaluador oficial de la planta "
        f"(`corpus.evaluar.evaluar(buscar, suite)`): {base['n']} preguntas de `evaluacion/retrieval.json`, "
        f"{len(a['corpus'].fragmentos)} fragmentos, k = 1, 3, 5 y 10. Salvo que se diga otra cosa, el buscador corre "
        "como lo usa el sistema: `solo_vigentes=True` (la Rev. C no se devuelve) y sin `equipo`.",
        "",
        "## Contra la línea de base",
        "",
        *_tabla([base, a["lexico"], elegido]),
        "",
        "- **distractor@5** es la métrica oficial: una sección que la suite marca como distractor de esa pregunta (la "
        "Rev. C, otro modelo, la lista de precios que no corresponde) aparece antes que cualquier relevante. Es la "
        "\"revisión vieja antes que la vigente en una de cada diez preguntas\" del README del corpus.",
        f"- **Rev. C antes que D**: preguntas en las que una sección de la Rev. C aparece antes que la misma sección "
        f"(misma clave) de la Rev. D, o sin ella, sea o no distractor de esa pregunta. En la línea de base son "
        f"{base['rev_c5']} de {base['n']}: los textos de la C y la D son casi iguales, empatan en BM25 y el empate se "
        "rompe a favor de la C, que está después en `fragmentos.jsonl`.",
        "- **BM25 propio + metadatos** es el mismo buscador sin embeddings: lo que queda si el modelo no carga.",
        "",
        "### La Rev. C, si se pide",
        "",
        f"Con `solo_vigentes=False` la Rev. C entra al ranking. Sin los ajustes por metadatos sale antes que su par de "
        f"la Rev. D en {crudo['rev_c5']} preguntas; con el ajuste `reemplazada` (la mitad del puntaje), en "
        f"{con_c['rev_c5']}"
        + (f" ({', '.join(con_c['ids_rev_c5'])}: la pregunta nombra algo que solo está en la Rev. C, como el código "
           "viejo 350-EMP-03)" if con_c["ids_rev_c5"] else "")
        + ". Si la consulta nombra la revisión (\"Rev. C\"), no se castiga.",
        "",
        *_tabla([crudo, con_c]),
        "",
        "## Modelos de embeddings",
        "",
        f"Medidos en esta PC ({platform.system()}, {os.cpu_count()} núcleos lógicos) con 2 hilos, el límite de CPU del "
        "contenedor del sistema, cada modelo en su proceso. Memoria: lo que suma cargar el modelo y el pico del proceso "
        "al indexar (con Python y ONNX Runtime). Calidad: solo vectores (con los mismos ajustes por metadatos) y el "
        "híbrido con BM25. Tokens: hasta dónde mira cada texto.",
        "",
        "| Modelo | Dim. | Archivos | Tokens | Solo vectores hit@5 / recall@5 | Híbrido hit@5 / recall@5 / MRR | "
        "Carga | Indexar 701 | Consulta | Memoria modelo / pico |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *filas_modelos,
        "",
        "Se embebe la sección sin el nombre del documento (`Fragmento.para_embeber`): en una prueba aparte con jina, el "
        "nombre del manual repetido en sus 112 secciones bajaba solo vectores de hit@5 0,83 a 0,76, porque acerca entre "
        "sí todas las secciones de un mismo manual. BM25 sí lo usa (`Fragmento.para_bm25`), y ahí suma.",
        "",
        "## Por tipo de pregunta",
        "",
        f"| Tipo | n | recall@5 BM25 | recall@5 {corto} | hit@5 BM25 | hit@5 {corto} |",
        "|---|---|---|---|---|---|",
        *por_tipo,
        "",
        "## Códigos de falla",
        "",
        f"{con['n']} preguntas armadas con la tabla del MBP-4 §6.1 (no son de la suite oficial): \"Apareció la falla "
        "TX-T07, ¿qué hay que hacer?\" tiene que traer la falla del catálogo de la planta (MBP-4 §6.11, F09) y "
        "\"¿Qué dice el manual del fabricante sobre la falla F09?\", la sección del fabricante (TX350-D §8.9).",
        "",
        "| | hit@1 | hit@3 | recall@3 | MRR |",
        "|---|---|---|---|---|",
        f"| Con la traducción (al indexar y en la consulta) | {_n(con['total']['hit@1'])} | "
        f"{_n(con['total']['hit@3'])} | {_n(con['total']['recall@3'])} | {_n(con['total']['mrr'])} |",
        f"| Sin la traducción | {_n(sin['total']['hit@1'])} | {_n(sin['total']['hit@3'])} | "
        f"{_n(sin['total']['recall@3'])} | {_n(sin['total']['mrr'])} |",
        "",
        "Con la traducción, la primera suele ser la sección del MBP-4 y la segunda la del fabricante (por eso hit@1 "
        "queda en la mitad): las dos sirven.",
    ]
    if a.get("pgvector"):
        pg = a["pgvector"]
        lineas += [
            "", "## Backend pgvector", "",
            f"Los mismos vectores, guardados en Postgres 16 con pgvector ({len(a['corpus'].fragmentos)} filas en "
            f"{_segundos(pg['guardar_s'])} s) y buscados con SQL (`ORDER BY embedding <=> consulta`): hit@5 "
            f"{_n(pg['r']['total']['hit@5'])}, recall@5 {_n(pg['r']['total']['recall@5'])}, MRR "
            f"{_n(pg['r']['total']['mrr'])}, y el mismo top 10 que el backend en memoria en {pg['iguales']} de "
            f"{base['n']} preguntas. Cada búsqueda tarda {_segundos(pg['consulta_ms'])} ms sin contar el embedding "
            f"de la consulta ({_segundos(info['consulta_ms'])} ms con {corto} y 2 hilos)."]
    lineas += [
        "", "## Conclusiones", "",
        f"- **Contra BM25**: hit@5 {_n(b['hit@5'])} → {_n(e['hit@5'])}, recall@5 {_n(b['recall@5'])} → "
        f"{_n(e['recall@5'])}, MRR {_n(b['mrr'])} → {_n(e['mrr'])} y distractor@5 {_n(b['distractor@5'])} → "
        f"{_n(e['distractor@5'])}. La mejora grande está en las preguntas de `{mejora}` (recall@5 "
        f"{_n(base['por_tipo'][mejora]['recall@5'])} → {_n(tipos[mejora]['recall@5'])}); la más chica, en el "
        f"historial ({_n(base['por_tipo']['historial']['recall@5'])} → {_n(tipos['historial']['recall@5'])}): las "
        "órdenes son texto libre con abreviaturas y a veces lo que las une con la pregunta es la causa, no las palabras "
        "(his-03: las válvulas gastadas con arena las explica un cartucho roto del F-201B).",
        f"- **Híbrido**: ni BM25 solo (hit@5 {_n(lexico['hit@5'])}, recall@5 {_n(lexico['recall@5'])}) ni los vectores "
        f"solos (hit@5 {_n(solo['hit@5'])}, recall@5 {_n(solo['recall@5'])}) llegan a la combinación. BM25 resuelve "
        "lo que tiene códigos, números o nombres propios (TX-VAL-KIT, 5V-1800, Servipat) y los vectores lo dicho con "
        "otras palabras (\"¿a qué hora son las recorridas?\" → MBP-4 §4.2, \"Horario de recorridas\"). Un modelo flojo "
        "resta: con MiniLM el híbrido queda abajo del mismo buscador sin vectores en MRR (tabla de modelos).",
        f"- **Rev. C**: la línea de base pone una sección de la Rev. C antes que la misma de la Rev. D en "
        f"{base['rev_c5']} de {base['n']} preguntas (top 5) y un distractor antes que lo relevante en "
        f"{round(b['distractor@5'] * base['n'])}; el buscador, en {elegido['rev_c5']} y "
        f"{round(e['distractor@5'] * base['n'])}. La Rev. C está reemplazada (MBP-4 §1.2: sus límites de vibración, "
        "temperatura y presión, el aceite ISO VG 150 y los códigos 350-xxx ya no valen; en TX350-C §3.2 la vibración "
        "dispara a 7,1 mm/s y en TX350-D §3.2 a 11,2), así que por defecto no se devuelve y, si se pide, va abajo de la "
        "D.",
        "- **Otros modelos y plantas**: TX350-D §3.3 (TX-250 y TX-500), MBP-4 §5.3 y §17 (PIA-1 y PIA-2), los "
        "opcionales (TX350-D §12, FC48-3 §16) y la instalación o preservación (TX350-D §4 y §11) quedan marcados como "
        "que no aplican, con el motivo, y bajan salvo que la consulta nombre ese modelo o esa planta. Las tablas de la "
        "familia (TX350-D §2.1, FC48-3 §2.1) bajan un poco: la velocidad máxima de la TX-350 sale de TX350-D §3.1 y no "
        "de la tabla que la compara con la TX-250 y la TX-500.",
        f"- **Códigos de falla**: la correspondencia sale del MBP-4 §6.1 (TX-T07 = F09, FC-T02 = F08, TX-T08 y FC-T04 "
        f"= F10). Con la traducción, hit@3 {_n(con['total']['hit@3'])} en las {con['n']} preguntas de códigos; sin "
        f"ella, {_n(sin['total']['hit@3'])}: el código solo no dice de qué falla se trata (\"TX-T07\" trae la tabla "
        "de fallas, TX350-D §8.2, y no la sección de la cavitación) y \"F09\" no encuentra la sección de Trimax, que "
        "no lo nombra.",
        f"- **Modelo**: `{info['modelo']}`. " + a["razon"],
        "- **Límites y stock no salen del RAG**: el buscador encuentra las secciones (límites: recall@5 "
        f"{_n(tipos['limites']['recall@5'])}), pero el dato que vale es el de GET /api/planta: el MBP-4 §5 dice que los "
        "límites de alarma configurados son los únicos válidos para PIA-3, y el MBP-4 §14.2 que el stock de su tabla "
        "es de referencia y el real es el del sistema de almacén.",
        "- **Ajustes** (`sistema.rag.busqueda.Ajustes`): elegidos con esta misma suite, sin un conjunto aparte para "
        "validar. En la zona probada (peso de los vectores 0,4 a 0,6; equipo y pasado 1 a 1,6) hit@5 y recall@5 se "
        "mueven una o dos preguntas.",
        "",
        "| Ajuste | Valor |",
        "|---|---|",
        *[f"| `{k}` | {str(v).replace('.', ',')} |" for k, v in a["ajustes"].__dict__.items()],
    ]
    return "\n".join(lineas) + "\n"


def elegir(modelos: list[dict]) -> tuple[dict, str]:
    """El de mejor recall@5 híbrido entre los que entran en la planta (archivos de hasta ARCHIVOS_MAX_GB y consulta
    de hasta CONSULTA_MAX_MS con 2 hilos). Si otro queda a menos de una pregunta, el más liviano."""
    def entra(m: dict) -> bool:
        return _gb(m["info"]["modelo"]) <= ARCHIVOS_MAX_GB and m["info"]["consulta_ms"] <= CONSULTA_MAX_MS

    def recall(m: dict) -> float:
        return m["hibrido"]["total"]["recall@5"]

    candidatos = [m for m in modelos if entra(m)] or modelos
    mejor = max(candidatos, key=lambda m: (recall(m), m["hibrido"]["total"]["mrr"]))
    elegido = min((m for m in candidatos if recall(mejor) - recall(m) <= 1 / 71 + 1e-9),
                  key=lambda m: _gb(m["info"]["modelo"]))
    e, i = elegido["hibrido"]["total"], elegido["info"]
    otros = []
    for m in modelos:
        if m is elegido:
            continue
        h, nombre = m["hibrido"]["total"], m["info"]["modelo"].split("/")[-1]
        notas = []
        if abs(recall(m) - e["recall@5"]) <= 1 / 71:
            notas.append("lo mismo que el elegido, a menos de una pregunta")
        if _gb(m["info"]["modelo"]) > ARCHIVOS_MAX_GB:
            notas.append(f"pero pesa más de {_n(ARCHIVOS_MAX_GB, 0)} GB")
        if m["info"]["consulta_ms"] > CONSULTA_MAX_MS:
            notas.append(f"pero tarda más de {CONSULTA_MAX_MS} ms por consulta")
        if MODELOS.get(m["info"]["modelo"], (0, 0))[1] <= 128:
            notas.append(f"solo mira los primeros {MODELOS[m['info']['modelo']][1]} tokens de cada sección")
        otros.append(f"`{nombre}` da recall@5 {_n(h['recall@5'])} y MRR {_n(h['mrr'])} en el híbrido, con "
                     f"{_n(_gb(m['info']['modelo']))} GB de archivos" + (f" ({', '.join(notas)})" if notas else ""))
    razon = (f"Elegido con datos: el mejor recall@5 híbrido entre los modelos que entran en la PC de la planta "
             f"(archivos de hasta {_n(ARCHIVOS_MAX_GB, 0)} GB y consulta de hasta {CONSULTA_MAX_MS} ms con 2 hilos) y, "
             f"si otro queda a menos de una pregunta, el más liviano. Da hit@5 {_n(e['hit@5'])}, recall@5 "
             f"{_n(e['recall@5'])} y MRR {_n(e['mrr'])}; consulta en {_segundos(i['consulta_ms'])} ms, indexa los 701 "
             f"fragmentos en {_segundos(i['indexado_s'])} s (una sola vez: queda en Postgres) con un pico de "
             f"{i['memoria_pico_mb']:.0f} MB, y es un modelo entrenado para castellano. "
             f"En cambio, {'; '.join(otros)}.")
    return elegido, razon


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modelos", nargs="+", default=list(MODELOS))
    ap.add_argument("--hilos", type=int, default=2)
    ap.add_argument("--lote", type=int, default=8)
    ap.add_argument("--remedir", action="store_true", help="vuelve a indexar con cada modelo aunque esté guardado")
    ap.add_argument("--database-url", help="también indexa en este Postgres y evalúa con el backend pgvector")
    ap.add_argument("--medir", help=argparse.SUPPRESS)
    ap.add_argument("--salida", type=Path, default=SALIDA)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.medir:
        medir_modelo(a.medir, a.hilos, a.lote)
        return

    corpus, suite = cargar()
    por_cita = {f.cita: f for f in corpus.fragmentos}
    lineas = [json.loads(l) for l in (DOCUMENTOS / "fragmentos.jsonl").read_text(encoding="utf-8").splitlines() if l]
    ajustes = Ajustes()
    sin_ajustes = replace(ajustes, no_aplica=1, nombra_otros=1, reemplazada=1, equipo_nombrado=1, fuera_de_periodo=1,
                          pasado=1)

    print("Sin embeddings:")
    base = medir("BM25 de la planta (línea de base)", BM25(lineas).buscar, suite, por_cita)
    solo_bm25 = Buscador(corpus, None, None, ajustes)
    lexico = medir("BM25 propio + metadatos (sin embeddings)", lambda q, k: solo_bm25.citas(q, k), suite, por_cita)

    modelos = []
    for modelo in a.modelos:
        matriz, info = medicion(modelo, a.hilos, a.lote, a.remedir)
        embebedor = ConsultasMedidas(modelo, suite)
        print(f"{modelo}:")
        solo = Buscador.en_memoria(corpus, embebedor, matriz, replace(ajustes, peso_vectores=1.0))
        hibrido = Buscador.en_memoria(corpus, embebedor, matriz, ajustes)
        modelos.append({"info": info, "embebedor": embebedor, "matriz": matriz, "buscador": hibrido,
                        "vectores": medir("solo vectores + metadatos", lambda q, k: solo.citas(q, k), suite, por_cita),
                        "hibrido": medir("híbrido + metadatos", lambda q, k: hibrido.citas(q, k), suite, por_cita)})

    mejor, razon = elegir(modelos)
    corto = mejor["info"]["modelo"].split("/")[-1]
    buscador = mejor["buscador"]
    print(f"Elegido: {mejor['info']['modelo']}")
    elegido = dict(mejor["hibrido"], nombre=f"**{corto}: híbrido + metadatos (elegido)**")
    crudo = Buscador.en_memoria(corpus, mejor["embebedor"], mejor["matriz"], sin_ajustes)
    con_c_sin_ajustes = medir(f"{corto}, `solo_vigentes=False`, sin ajustes por metadatos",
                              lambda q, k: crudo.citas(q, k, False), suite, por_cita)
    con_c = medir(f"{corto}, `solo_vigentes=False`, con los ajustes", lambda q, k: buscador.citas(q, k, False),
                  suite, por_cita)

    codigos = suite_codigos(corpus)
    con = evaluar(lambda q, k: buscador.citas(q, k), codigos, (1, 3))
    corpus_sin, matriz_sin = sin_traduccion(corpus, mejor["matriz"], mejor["embebedor"])
    buscador_sin = Buscador.en_memoria(corpus_sin, mejor["embebedor"], matriz_sin, ajustes)
    sin = evaluar(lambda q, k: buscador_sin.citas(q, k), codigos, (1, 3))
    print(f"Códigos de falla: hit@3 {con['total']['hit@3']} con la traducción, {sin['total']['hit@3']} sin", flush=True)

    pg = None
    if a.database_url:
        pg = evaluar_pgvector(a.database_url, corpus, mejor["matriz"], mejor["embebedor"], buscador, suite, por_cita)

    a.salida.write_text(informe({
        "corpus": corpus, "base": base, "lexico": lexico, "modelos": modelos, "elegido": elegido,
        "elegido_info": mejor["info"], "razon": razon, "con_c_sin_ajustes": con_c_sin_ajustes, "con_c": con_c,
        "codigos": (con, sin), "pgvector": pg, "ajustes": ajustes}), encoding="utf-8")
    print(f"Resultados en {a.salida}")


if __name__ == "__main__":
    main()

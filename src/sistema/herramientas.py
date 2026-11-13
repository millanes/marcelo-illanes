"""Las herramientas (tools) de solo lectura del sistema: lo que puede consultar un modelo de lenguaje.

Seis tools, cada una con un modelo de entrada Pydantic estricto (extra="forbid": un argumento de más es un error,
no se ignora) y una salida tipada:

    equipos              equipos, nombres informales, sensores y límites de alarma       GET /api/planta
    historial_sensores   un sensor en una ventana de hasta 7 días, resumida              GET /api/historian
    historial_ordenes    órdenes de trabajo del archivo y del simulador                  GET /api/historial-ordenes
    materiales           repuestos, stock por almacén y lista de materiales por equipo   datos/materiales.yaml
    catalogo_tareas      tareas estándar y no estándar del plan de mantenimiento         datos/materiales.yaml
    buscar_manuales      fragmentos de manuales, buenas prácticas, contrato e historial  el buscador del RAG

Ninguna escribe nada. Lo de la planta va por ClientePlanta, con timeout y reintentos ante errores de red y 5xx (el
historian, por ejemplo, a veces contesta 500 si se lo lee justo mientras escribe). Si aun así falla, la tool no
levanta la excepción: devuelve su salida con `error` (qué pasó, en castellano, y si tiene sentido reintentar), para
que el modelo lo pueda explicar o probar otra cosa. Lo que devuelven está acotado (ventana máxima, `limite`, textos
recortados) para no llenarle el contexto al modelo.

La hora es la de la planta, UTC−3: el simulador corre acelerado y con fecha propia. Un `hasta` sin huso se toma en
esa hora y, sin `hasta`, se usa la hora actual de la planta, que se lee de GET /api/estado (solo el campo `ts`).
"""

from __future__ import annotations

import functools
import inspect
import logging
import math
import os
import re
import statistics
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, TypeVar
from urllib.parse import quote, urlencode

import httpx
import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .equipos import Catalogo
from .planta import ClientePlanta
from .texto import normalizar

log = logging.getLogger(__name__)

# America/Argentina/Buenos_Aires: UTC−3 todo el año. El historian compara las horas como vienen, sin pasarlas a su
# huso (pedirle 18:00Z devuelve las 18:00 de la planta): todo lo que se le pide va en UTC−3.
HUSO_PLANTA = timezone(timedelta(hours=-3))
VENTANA_MAXIMA_H = 168     # historial_sensores: 7 días, 10 080 lecturas de un minuto
TRAMOS_MAXIMOS = 120
ORDENES_MAXIMAS = 50
FRAGMENTOS_MAXIMOS = 10
TEXTO_MAXIMO = 1500        # caracteres de cada fragmento de manual y de cada texto de una orden

PATRON_TAG = r"^[A-Za-z]{1,3}-?\d{3}[A-Za-z]?$"
PATRON_CODIGO = r"^[A-Za-z0-9]{1,8}(-[A-Za-z0-9]{1,8}){0,3}$"


def ruta_materiales() -> Path:
    """datos/materiales.yaml: el de MATERIALES_YAML, el de la carpeta donde se corre (/app en la imagen) o el del
    repo."""
    if os.environ.get("MATERIALES_YAML"):
        return Path(os.environ["MATERIALES_YAML"])
    local = Path.cwd() / "datos" / "materiales.yaml"
    return local if local.exists() else Path(__file__).resolve().parents[2] / "datos" / "materiales.yaml"


# --- Tipos de los campos ---------------------------------------------------------------------------------------

def _tag(valor: str) -> str:
    """'b101' -> 'B-101'. La forma ya la garantiza el patrón."""
    letras, numero = re.fullmatch(r"([A-Za-z]{1,3})-?(\d{3}[A-Za-z]?)", valor).groups()
    return f"{letras}-{numero}".upper()


def _tag_o_planta(valor: str) -> str:
    return "PLANTA" if valor.upper() == "PLANTA" else _tag(valor)


Tag = Annotated[str, Field(pattern=PATRON_TAG), AfterValidator(_tag)]
TagOPlanta = Annotated[str, Field(pattern=r"^([A-Za-z]{1,3}-?\d{3}[A-Za-z]?|PLANTA|[Pp]lanta)$"),
                       AfterValidator(_tag_o_planta)]
Codigo = Annotated[str, Field(pattern=PATRON_CODIGO), AfterValidator(str.upper)]
Texto = Annotated[str, Field(min_length=2, max_length=80)]
Numero = int | float


class Estricto(BaseModel):
    """Base de las entradas y salidas de las tools: un campo que no está en el modelo es un error."""
    model_config = ConfigDict(extra="forbid")


# --- Errores ---------------------------------------------------------------------------------------------------

TipoError = Literal["sin_conexion", "timeout", "error_planta", "pedido_rechazado", "respuesta_invalida",
                    "no_encontrado", "entrada_invalida", "sin_datos", "sin_buscador", "error_buscador",
                    "error_interno"]


class ErrorHerramienta(Estricto):
    """Por qué la tool no pudo contestar. Viaja en la salida, en lugar de una excepción."""
    tipo: TipoError = Field(description=(
        "sin_conexion: no se pudo conectar con la planta. timeout: la planta (o el buscador) no contestó a tiempo. "
        "error_planta: la planta contestó con un error suyo (5xx). pedido_rechazado: la planta rechazó el pedido "
        "(4xx). respuesta_invalida: la planta contestó algo que no se entiende. no_encontrado: el equipo, sensor, "
        "repuesto o tarea no existe. entrada_invalida: los argumentos no son válidos. sin_datos: no se pudo leer "
        "el catálogo de materiales. sin_buscador / error_buscador: la búsqueda en manuales no está disponible o "
        "falló. error_interno: falla del sistema."))
    mensaje: str = Field(description="Qué pasó, para explicárselo a la persona o corregir el pedido")
    reintentable: bool = Field(description="Si tiene sentido volver a hacer el mismo pedido más tarde")
    estado_http: int | None = Field(None, description="Código HTTP con que contestó la planta, si contestó")


class FallaHerramienta(Exception):
    """Una falla prevista (un equipo que no existe, no hay buscador...): la tool la devuelve como error."""

    def __init__(self, tipo: TipoError, mensaje: str, reintentable: bool = False) -> None:
        super().__init__(mensaje)
        self.error = ErrorHerramienta(tipo=tipo, mensaje=mensaje, reintentable=reintentable)


class Salida(Estricto):
    """Base de las salidas: los datos pedidos o, si la tool no pudo contestar, `error`."""
    error: ErrorHerramienta | None = Field(None, description=(
        "Si no es null, la tool no pudo contestar (el resto viene vacío) y acá dice por qué"))


_MENSAJES_VALIDACION = {
    "extra_forbidden": "no es un argumento de esta tool",
    "missing": "falta y es obligatorio",
    "string_pattern_mismatch": "no tiene el formato esperado",
    "literal_error": "no es uno de los valores posibles",
}


def explicar(e: ValidationError) -> str:
    """Los errores de validación de una entrada, en castellano: 'Argumentos inválidos. otro: no es un argumento...'."""
    partes = []
    for err in e.errors():
        campo = ".".join(str(p) for p in err["loc"])
        motivo = _MENSAJES_VALIDACION.get(err["type"], err["msg"].removeprefix("Value error, "))
        partes.append(f"{campo}: {motivo}" if campo else motivo)
    return "Argumentos inválidos. " + "; ".join(partes)


# --- equipos ---------------------------------------------------------------------------------------------------

class EntradaEquipos(Estricto):
    tag: Tag | None = Field(None, description="TAG de un equipo (B-101, F-201A, TK-301...) para traer solo ese")
    tipo: Literal["bomba", "filtro", "tanque", "caudalimetro"] | None = Field(
        None, description="Solo los equipos de este tipo")
    nombre: Texto | None = Field(None, description=(
        "Nombre informal como lo dicen en campo ('la dos', 'la del medio', 'el de la izquierda', 'el pulmón'): "
        "trae el equipo que nombra. 'La bomba' a secas no nombra a ninguno: hay tres"))


class Sensor(Estricto):
    variable: str = Field(description="Variable del sensor: la que se le pide a historial_sensores")
    unidad: str | None = None
    alarma_alta: float | None = None
    alarma_baja: float | None = None
    disparo: float | None = Field(None, description="Valor al que la protección para el equipo")


class FichaEquipo(Estricto):
    tag: str
    tipo: str
    descripcion: str | None = None
    fabricante: str | None = None
    modelo: str | None = None
    nombres: list[str] = Field(default_factory=list, description="Cómo lo nombran en campo")
    unidad_manometro: str | None = Field(None, description=(
        "Unidad del manómetro local de la bomba (kg/cm2, psi o bar): en la que el operador dicta la descarga"))
    rol_inicial: str | None = Field(None, description="servicio o reserva al arrancar la planta (no el de ahora)")
    caudal_nominal_m3d: float | None = None
    cartuchos: int | None = None
    altura_m: float | None = None
    diametro_m: float | None = None
    sensores: list[Sensor] = Field(default_factory=list, description=(
        "Sensores conectados al sistema de control, con sus límites de alarma y disparo"))


class SalidaEquipos(Salida):
    planta: str | None = None
    inyeccion_programada_m3d: float | None = None
    equipos: list[FichaEquipo] = Field(default_factory=list)


# --- historial_sensores ----------------------------------------------------------------------------------------

class EntradaHistorialSensores(Estricto):
    tag: Tag = Field(description="TAG del equipo (B-101, F-201A, FT-401...)")
    variable: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$", description=(
        "Variable del sensor, como la lista la tool equipos: presion_succion, presion_descarga, temperatura_aceite "
        "y vibracion en las bombas; presion_diferencial en los filtros; caudal en el FT-401"))
    hasta: datetime | None = Field(None, description=(
        "Fin de la ventana, en hora de la planta (ISO 8601, por ejemplo 2026-11-12T14:00:00-03:00). Vacío: ahora, "
        "la hora actual de la planta (no la de tu máquina: el simulador tiene fecha propia)"))
    horas: int = Field(6, ge=1, le=VENTANA_MAXIMA_H, description=(
        f"Largo de la ventana hacia atrás desde `hasta`, en horas: de 1 a {VENTANA_MAXIMA_H} (7 días)"))
    tramos: int = Field(24, ge=1, le=TRAMOS_MAXIMOS, description=(
        f"En cuántos tramos resumir la serie (promedio, mínimo y máximo de cada uno), de 1 a {TRAMOS_MAXIMOS}. "
        "Hay una lectura por minuto: 6 horas en 24 tramos son tramos de 15 minutos"))

    @field_validator("variable", mode="before")
    @classmethod
    def _sin_tildes(cls, valor: Any) -> Any:
        """'Vibración' -> 'vibracion'."""
        return normalizar(valor).replace(" ", "_") if isinstance(valor, str) else valor


class Tramo(Estricto):
    desde: datetime = Field(description="Primera lectura del tramo")
    hasta: datetime = Field(description="Última lectura del tramo")
    promedio: float
    minimo: float
    maximo: float


class ResumenSerie(Estricto):
    lecturas: int = Field(description="Cuántas lecturas hay en la ventana (una por minuto si el sensor anduvo)")
    minimo: float
    ts_minimo: datetime
    maximo: float
    ts_maximo: datetime
    promedio: float
    primera: float = Field(description="Primera lectura de la ventana")
    ultima: float = Field(description="Última lectura de la ventana")
    tendencia: Literal["subiendo", "bajando", "estable"] = Field(description=(
        "Promedio del último cuarto de la ventana contra el del primero: más de 5 % de diferencia es subiendo o "
        "bajando"))
    sobre_alarma_alta: int | None = Field(None, description="Lecturas por encima de la alarma alta (null: no tiene)")
    bajo_alarma_baja: int | None = Field(None, description=(
        "Lecturas por debajo de la alarma baja (null: no tiene). Una bomba parada lee bajo y no es una alarma"))


class SalidaHistorialSensores(Salida):
    tag: str | None = None
    sensor: Sensor | None = Field(None, description="El sensor, con su unidad y sus límites")
    desde: datetime | None = None
    hasta: datetime | None = None
    resumen: ResumenSerie | None = Field(None, description="null si no hay lecturas en la ventana")
    tramos: list[Tramo] = Field(default_factory=list, description="La serie reducida, de la más vieja a la más nueva")


# --- historial_ordenes -----------------------------------------------------------------------------------------

class EntradaHistorialOrdenes(Estricto):
    equipo: TagOPlanta | None = Field(None, description=(
        "TAG del equipo (B-101, F-201A...) o PLANTA, las órdenes generales (por ejemplo, la medición mensual de "
        "vibraciones de las tres bombas). Vacío: todas"))
    desde: date | None = Field(None, description="Primer día, inclusive (AAAA-MM-DD)")
    hasta: date | None = Field(None, description="Último día, inclusive (AAAA-MM-DD)")
    texto: Texto | None = Field(None, description=(
        "Palabra a buscar en el aviso, el trabajo realizado y la descripción, sin importar mayúsculas ni tildes. "
        "Lo escribió la gente de campo, con abreviaturas: probá con el principio de la palabra ('empaq', 'vibra')"))
    tarea: Codigo | None = Field(None, description="Solo las órdenes con esta tarea (T-B-EMP, T-F-CAR, I-VIB...)")
    codigo_falla: str | None = Field(None, pattern=r"^[Ff]\d{2}$", description=(
        "Solo las órdenes con este código de falla (F01 a F10). Casi la mitad de las órdenes no lo tiene cargado"))
    limite: int = Field(20, ge=1, le=ORDENES_MAXIMAS, description=(
        f"Cuántas órdenes devolver como máximo (1 a {ORDENES_MAXIMAS}), las más recientes primero"))

    @field_validator("codigo_falla")
    @classmethod
    def _mayuscula(cls, valor: str | None) -> str | None:
        return valor.upper() if valor else valor

    @model_validator(mode="after")
    def revisar_fechas(self) -> EntradaHistorialOrdenes:
        if self.desde and self.hasta and self.desde > self.hasta:
            raise ValueError("desde es posterior a hasta")
        return self


class RepuestoUsado(Estricto):
    codigo: str
    cantidad: Numero


class Orden(Estricto):
    id: str
    fecha: datetime = Field(description="Fecha del aviso")
    equipo: str
    tipo: str | None = Field(None, description="correctiva, por condición o predictiva")
    estado: str | None = None
    origen: str | None = Field(None, description="archivo (dos años de historial) o simulador (el precalentamiento)")
    tareas: list[str] = Field(default_factory=list)
    ejecutor: str | None = Field(None, description="propio o la contratista (Servipat)")
    codigo_falla: str | None = None
    aviso: str | None = None
    trabajo_realizado: str | None = None
    descripcion: str | None = None
    repuestos: list[RepuestoUsado] = Field(default_factory=list)
    horas: float | None = None
    fecha_inicio: datetime | None = None
    fecha_cierre: datetime | None = None


class SalidaHistorialOrdenes(Salida):
    total: int = Field(0, description="Cuántas órdenes cumplen el filtro: pueden ser más que las devueltas")
    ordenes: list[Orden] = Field(default_factory=list, description="Las más recientes primero, hasta `limite`")


# --- materiales y catalogo_tareas ------------------------------------------------------------------------------

class EntradaMateriales(Estricto):
    codigo: Codigo | None = Field(None, description="Código de un repuesto (TX-EMP-KIT, FC-CART-25...): trae solo ese")
    equipo: Tag | None = Field(None, description="TAG de un equipo: su lista de materiales (los repuestos que lleva)")
    texto: Texto | None = Field(None, description=(
        "Palabra a buscar en el nombre del repuesto ('empaquetadura', 'cartucho', 'aceite'), sin importar mayúsculas "
        "ni tildes"))
    con_stock_en: Literal["yacimiento", "central"] | None = Field(None, description="Solo los que hay en ese almacén")


class Repuesto(Estricto):
    codigo: str
    nombre: str
    unidad: str
    costo_usd: Numero = Field(description="Costo unitario en dólares")
    stock: dict[str, Numero] = Field(description=(
        "Stock por almacén: 'yacimiento' es el de la planta; 'central' está en Comodoro Rivadavia, a 3 a 5 días"))
    equipos: list[str] = Field(description="Equipos en cuya lista de materiales está")


class SalidaMateriales(Salida):
    repuestos: list[Repuesto] = Field(default_factory=list)


class EntradaCatalogoTareas(Estricto):
    codigo: Codigo | None = Field(None, description="Código de una tarea (T-B-EMP, T-F-CAR, I-VIB...)")
    equipo: Tag | None = Field(None, description=(
        "TAG de un equipo: las tareas estándar de ese equipo, más las no estándar (que no tienen equipo asignado)"))
    texto: Texto | None = Field(None, description="Palabra a buscar en el nombre ('empaquetadura', 'rodamientos')")
    estandar: bool | None = Field(None, description="true: solo las estándar; false: las no estándar; vacío: todas")


class RepuestoDeTarea(Estricto):
    codigo: str
    nombre: str | None = Field(None, description="null si el código no está en el catálogo de repuestos")
    cantidad: Numero
    costo_unitario_usd: Numero | None = None
    stock: dict[str, Numero] = Field(default_factory=dict, description="Stock por almacén, para ver si alcanza")


class Tarea(Estricto):
    codigo: str
    nombre: str
    estandar: bool = Field(description="Las no estándar siempre requieren aprobación del planificador")
    equipos: list[str] = Field(description="Equipos a los que aplica. Las no estándar no tienen equipos asignados")
    detiene: bool = Field(description="Si hay que parar el equipo para hacerla")
    horas: Numero = Field(description="Duración estimada, en horas")
    ejecutor: str = Field(description="propio (la gente de la planta) o contratista")
    repuestos: list[RepuestoDeTarea] = Field(default_factory=list)
    costo_repuestos_usd: Numero = Field(description="Costo de los repuestos de la tarea: unitario por cantidad")


class SalidaCatalogoTareas(Salida):
    tareas: list[Tarea] = Field(default_factory=list)


class _Cantidad(BaseModel):
    codigo: str
    cantidad: Numero


class _RepuestoCatalogo(BaseModel):
    codigo: str
    nombre: str
    unidad: str
    costo_usd: Numero
    stock: dict[str, Numero] = {}


class _TareaCatalogo(BaseModel):
    codigo: str
    nombre: str
    equipos: list[str] = []
    detiene: bool
    horas: Numero
    ejecutor: str
    repuestos: list[_Cantidad] = []


class CatalogoMateriales(BaseModel):
    """datos/materiales.yaml, validado: repuestos con su stock, lista de materiales por equipo y tareas."""
    repuestos: list[_RepuestoCatalogo]
    lista_materiales: dict[str, list[str]] = {}
    tareas_estandar: list[_TareaCatalogo] = []
    tareas_no_estandar: list[_TareaCatalogo] = []

    @classmethod
    def leer(cls, ruta: Path) -> CatalogoMateriales:
        return cls.model_validate(yaml.safe_load(ruta.read_text(encoding="utf-8")))


# --- buscar_manuales -------------------------------------------------------------------------------------------

class BuscadorManuales(Protocol):
    """Lo que tiene que tener el buscador del RAG para la tool buscar_manuales. Si además tiene `listo` (como
    rag.Manuales, que indexa en un hilo al arrancar) y es False, la tool contesta que el índice se está preparando
    en lugar de devolver una lista vacía."""

    def buscar(self, consulta: str, k: int = 5, solo_vigentes: bool = True,
               equipo: str | None = None) -> list[dict]:
        """Los k fragmentos más relevantes, cada uno con al menos cita, documento, titulo, texto, vigente y
        puntaje."""
        ...


class EntradaBuscarManuales(Estricto):
    consulta: str = Field(min_length=3, max_length=500, description=(
        "Qué buscar, como lo preguntaría alguien de la planta ('a qué vibración dispara la triplex', 'cada cuánto "
        "se cambian los cartuchos del filtro fino')"))
    k: int = Field(5, ge=1, le=FRAGMENTOS_MAXIMOS, description=f"Cuántos fragmentos traer (1 a {FRAGMENTOS_MAXIMOS})")
    solo_vigentes: bool = Field(True, description=(
        "Solo documentos vigentes. La Rev. C del manual de la bomba (TX350-C) está reemplazada por la Rev. D y "
        "tiene otros límites, otro aceite y otros códigos: pedila (false) solo para comparar"))
    equipo: Tag | None = Field(None, description="TAG del equipo, para priorizar sus documentos")


class Fragmento(Estricto):
    cita: str = Field(description="Cómo citarlo: 'TX350-D §3.2' o 'historial OT-25-0026'")
    documento: str
    titulo: str
    texto: str = Field(description=f"El texto de la sección, recortado a {TEXTO_MAXIMO} caracteres")
    vigente: bool
    puntaje: float = Field(description="Relevancia según el buscador: más alto, más relevante")


class SalidaBuscarManuales(Salida):
    fragmentos: list[Fragmento] = Field(default_factory=list, description="Del más relevante al menos relevante")


# --- Cliente de la planta --------------------------------------------------------------------------------------

class ClienteConsultas(ClientePlanta):
    """ClientePlanta con las dos consultas que usan solo las tools, con el mismo timeout y los mismos reintentos."""

    def historial_ordenes(self, equipo: str | None = None, desde: str | None = None,
                          hasta: str | None = None) -> list[dict]:
        """Órdenes de trabajo, de la más vieja a la más nueva (GET /api/historial-ordenes)."""
        filtros = {k: v for k, v in {"equipo": equipo, "desde": desde, "hasta": hasta}.items() if v}
        return self._get("/api/historial-ordenes" + (f"?{urlencode(filtros)}" if filtros else "")).json()

    def hora(self) -> datetime:
        """La hora de la planta. De GET /api/estado se usa solo `ts`: el resto es el estado interno del simulador."""
        return datetime.fromisoformat(self._get("/api/estado").json()["ts"])


# --- Las tools -------------------------------------------------------------------------------------------------

S = TypeVar("S", bound=Salida)


def _protegida(salida: type[S]) -> Callable[[Callable[..., S]], Callable[..., S]]:
    """Lo que falle adentro de la tool vuelve en su salida como ErrorHerramienta, nunca como excepción."""
    def decorar(metodo: Callable[..., S]) -> Callable[..., S]:
        @functools.wraps(metodo)
        def tool(self: Herramientas, entrada: Any) -> S:
            try:
                return metodo(self, entrada)
            except Exception as e:  # la frontera de la tool: de acá no sale ninguna excepción
                return salida(error=self._error(metodo.__name__, e))
        return tool
    return decorar


class Herramientas:
    """Las seis tools, sobre el cliente de la planta, el catálogo de materiales y el buscador de manuales (si hay).

    `hora_planta` da la hora actual de la planta (por ejemplo RelojPlanta.ahora, si corre al lado del bus); si no se
    pasa, se pregunta a la planta cada vez que hace falta.
    """

    def __init__(self, planta: ClienteConsultas, materiales: Path | str | None = None,
                 buscador: BuscadorManuales | None = None, hora_planta: Callable[[], datetime] | None = None,
                 timeout_buscador_s: float = 20.0, reintentos_buscador: int = 1,
                 espera_buscador_s: float = 0.5) -> None:
        self._planta = planta
        self._ruta_materiales = Path(materiales) if materiales else ruta_materiales()
        self._buscador = buscador
        self._hora_planta = hora_planta or planta.hora
        self.timeout_buscador_s, self.reintentos_buscador = timeout_buscador_s, reintentos_buscador
        self.espera_buscador_s = espera_buscador_s
        # Un solo hilo para el buscador: le da el timeout y no le hace llamados en paralelo.
        self._hilo_buscador = ThreadPoolExecutor(1, thread_name_prefix="buscador") if buscador is not None else None
        self._datos_planta: dict | None = None
        self._catalogo: CatalogoMateriales | None = None
        self._candado_planta, self._candado_catalogo = threading.Lock(), threading.Lock()

    def ejecutar(self, nombre: str, argumentos: dict[str, Any]) -> Salida:
        """Corre la tool `nombre` con los argumentos tal como los mandó el modelo. Si no validan, devuelve la salida
        con un error entrada_invalida que dice qué está mal. Una tool que no existe es KeyError."""
        definicion = DEFINICION[nombre]
        try:
            entrada = definicion.entrada.model_validate(argumentos)
        except ValidationError as e:
            log.info("Tool %s: argumentos inválidos: %s", nombre, explicar(e))
            return definicion.salida(error=ErrorHerramienta(tipo="entrada_invalida", mensaje=explicar(e),
                                                            reintentable=False))
        return getattr(self, nombre)(entrada)

    @_protegida(SalidaEquipos)
    def equipos(self, entrada: EntradaEquipos) -> SalidaEquipos:
        """Equipos de la planta PIA-3: TAG, tipo, fabricante y modelo, los nombres con que los nombran en campo, la
        unidad del manómetro local y los sensores conectados, con sus límites de alarma y de disparo.

        Usala para pasar de un nombre informal ("la del medio", "el filtro de la izquierda") al TAG, para saber qué
        variables tiene cada equipo antes de pedir su historial y para saber contra qué límites comparar una
        lectura. Los límites que valen son estos: los del manual Rev. C de la bomba están vencidos. Sin filtros
        trae todos los equipos (son siete).
        """
        datos = self._datos()
        fichas = [_ficha(e, datos.get("sensores", [])) for e in datos.get("equipos", [])]
        if entrada.tag:
            if entrada.tag not in {f.tag for f in fichas}:
                raise FallaHerramienta("no_encontrado", self._no_existe(entrada.tag, datos))
            fichas = [f for f in fichas if f.tag == entrada.tag]
        if entrada.nombre:
            tag = self._tag_de(entrada.nombre, datos)
            fichas = [f for f in fichas if f.tag == tag]
        if entrada.tipo:
            fichas = [f for f in fichas if f.tipo == entrada.tipo]
        planta = datos.get("planta") or {}
        return SalidaEquipos(planta=planta.get("nombre"),
                             inyeccion_programada_m3d=planta.get("inyeccion_programada_m3d"), equipos=fichas)

    @_protegida(SalidaHistorialSensores)
    def historial_sensores(self, entrada: EntradaHistorialSensores) -> SalidaHistorialSensores:
        """Historial de un sensor del sistema de control (una lectura por minuto) en una ventana de hasta 7 días:
        resumen (mínimo y máximo con su hora, promedio, primera y última lectura, tendencia y cuántas lecturas
        pasaron los límites de alarma) y la serie reducida a tramos con promedio, mínimo y máximo.

        La ventana termina en `hasta` y va `horas` para atrás; sin `hasta` termina ahora, en la hora de la planta.
        Solo hay historial de los sensores conectados (presiones, temperatura de aceite y vibración de las bombas,
        diferencial de los filtros y caudal del colector): las lecturas locales de la recorrida no están. Para ver
        qué sensores tiene cada equipo, usá la tool equipos.
        """
        sensor = self._sensor(entrada.tag, entrada.variable)
        hasta = _en_hora_planta(entrada.hasta or self._hora_planta()).replace(microsecond=0)
        desde = hasta - timedelta(hours=entrada.horas)
        crudas = self._planta.historian(entrada.tag, entrada.variable, quote(desde.isoformat()),
                                        quote(hasta.isoformat()))
        lecturas = _lecturas(_lista(crudas, "el historian"))
        return SalidaHistorialSensores(tag=entrada.tag, sensor=sensor, desde=desde, hasta=hasta,
                                       resumen=_resumen(lecturas, sensor), tramos=_tramos(lecturas, entrada.tramos))

    @_protegida(SalidaHistorialOrdenes)
    def historial_ordenes(self, entrada: EntradaHistorialOrdenes) -> SalidaHistorialOrdenes:
        """Órdenes de trabajo de la planta: dos años de archivo, escritas por la gente de campo (con abreviaturas,
        sin tildes y con el código de falla sin cargar en casi la mitad), más las del simulador. Cada una con
        fecha, equipo, tipo, tareas, aviso, trabajo realizado, repuestos, horas y estado.

        Usala para ver qué se le hizo a un equipo, cada cuánto se repite una falla o qué se cambió la última vez:
        el historial a veces muestra que la causa de fondo no es la primera que da el manual. Devuelve las más
        recientes primero, hasta `limite`, y en `total` cuántas cumplen el filtro.
        """
        if entrada.equipo and entrada.equipo != "PLANTA":
            datos = self._datos()
            if entrada.equipo not in {e.get("tag") for e in datos.get("equipos", [])}:
                raise FallaHerramienta("no_encontrado", self._no_existe(entrada.equipo, datos))
        desde = _en_hora_planta(datetime.combine(entrada.desde, datetime.min.time())) if entrada.desde else None
        hasta = _en_hora_planta(datetime.combine(entrada.hasta, datetime.max.time())) if entrada.hasta else None
        crudas = _lista(self._planta.historial_ordenes(
            entrada.equipo, desde and desde.isoformat(), hasta and hasta.replace(microsecond=0).isoformat()),
            "el historial de órdenes")
        ordenes = [orden for o in crudas if (orden := _orden(o)) is not None]
        if entrada.texto:
            buscado = _buscado(entrada.texto)
            ordenes = [o for o in ordenes if buscado in normalizar(" ".join(
                t for t in (o.aviso, o.trabajo_realizado, o.descripcion) if t))]
        if entrada.tarea:
            ordenes = [o for o in ordenes if entrada.tarea in o.tareas]
        if entrada.codigo_falla:
            ordenes = [o for o in ordenes if o.codigo_falla == entrada.codigo_falla]
        ordenes.sort(key=lambda o: o.fecha, reverse=True)
        return SalidaHistorialOrdenes(total=len(ordenes), ordenes=ordenes[:entrada.limite])

    @_protegida(SalidaMateriales)
    def materiales(self, entrada: EntradaMateriales) -> SalidaMateriales:
        """Repuestos y materiales: código, nombre, unidad, costo unitario en dólares, stock por almacén y en qué
        equipos se usan. Con `equipo` es la lista de materiales de ese equipo: los repuestos que le corresponden
        (una orden no debería llevar repuestos de fuera de esa lista).

        El almacén "yacimiento" es el de la planta; "central" está en Comodoro Rivadavia, a 3 a 5 días. Sin
        filtros trae todo el catálogo (son once repuestos).
        """
        catalogo = self._materiales()
        usos: dict[str, list[str]] = {}
        for tag, codigos in catalogo.lista_materiales.items():
            for codigo in codigos:
                usos.setdefault(codigo, []).append(tag)
        repuestos = [Repuesto(codigo=r.codigo, nombre=r.nombre, unidad=r.unidad, costo_usd=r.costo_usd,
                              stock=dict(r.stock), equipos=usos.get(r.codigo, [])) for r in catalogo.repuestos]
        if entrada.codigo:
            repuestos = [r for r in repuestos if r.codigo == entrada.codigo]
            if not repuestos:
                raise FallaHerramienta("no_encontrado", f"No hay ningún repuesto {entrada.codigo}. Buscalo por "
                                                        "nombre con `texto` o pedí el catálogo sin filtros.")
        if entrada.equipo:
            if entrada.equipo not in catalogo.lista_materiales:
                raise FallaHerramienta("no_encontrado", f"{entrada.equipo} no tiene lista de materiales. La tienen: "
                                                        f"{', '.join(catalogo.lista_materiales)}.")
            repuestos = [r for r in repuestos if r.codigo in catalogo.lista_materiales[entrada.equipo]]
        if entrada.texto:
            buscado = _buscado(entrada.texto)
            repuestos = [r for r in repuestos if buscado in normalizar(f"{r.nombre} {r.codigo}")]
        if entrada.con_stock_en:
            repuestos = [r for r in repuestos if r.stock.get(entrada.con_stock_en, 0) > 0]
        return SalidaMateriales(repuestos=repuestos)

    @_protegida(SalidaCatalogoTareas)
    def catalogo_tareas(self, entrada: EntradaCatalogoTareas) -> SalidaCatalogoTareas:
        """Catálogo de tareas del plan de mantenimiento: código, nombre, si es estándar, a qué equipos aplica, si
        hay que parar el equipo, horas, quién la hace y qué repuestos lleva (con su costo y el stock de cada
        almacén).

        Las tareas no estándar (rodamientos, alineación, análisis de vibraciones, revisión de válvulas) no tienen
        equipo asignado y siempre requieren aprobación del planificador. Una misma tarea puede aparecer dos veces
        con distintos repuestos según el equipo (el cambio de cartuchos del filtro A y del B).
        """
        catalogo = self._materiales()
        repuestos = {r.codigo: r for r in catalogo.repuestos}
        tareas = ([_tarea(t, True, repuestos) for t in catalogo.tareas_estandar]
                  + [_tarea(t, False, repuestos) for t in catalogo.tareas_no_estandar])
        if entrada.codigo:
            tareas = [t for t in tareas if t.codigo == entrada.codigo]
            if not tareas:
                raise FallaHerramienta("no_encontrado", f"No hay ninguna tarea {entrada.codigo}. Buscala por nombre "
                                                        "con `texto` o pedí el catálogo sin filtros.")
        if entrada.equipo:
            con_tareas = {e for t in catalogo.tareas_estandar for e in t.equipos}
            if entrada.equipo not in con_tareas:
                raise FallaHerramienta("no_encontrado", f"{entrada.equipo} no tiene tareas estándar. Las tienen: "
                                                        f"{', '.join(sorted(con_tareas))}.")
            tareas = [t for t in tareas if not t.estandar or entrada.equipo in t.equipos]
        if entrada.texto:
            buscado = _buscado(entrada.texto)
            tareas = [t for t in tareas if buscado in normalizar(f"{t.nombre} {t.codigo}")]
        if entrada.estandar is not None:
            tareas = [t for t in tareas if t.estandar == entrada.estandar]
        return SalidaCatalogoTareas(tareas=tareas)

    @_protegida(SalidaBuscarManuales)
    def buscar_manuales(self, entrada: EntradaBuscarManuales) -> SalidaBuscarManuales:
        """Búsqueda en los documentos de la planta: manuales de la bomba triplex TX-350 y del filtro FC-48, el
        manual de buenas prácticas MBP-4, el contrato con Servipat y el historial de órdenes. Devuelve los
        fragmentos más relevantes, cada uno con su cita ("TX350-D §3.2"), para fundamentar una respuesta.

        Citá siempre de dónde sale lo que digas. Por defecto trae solo documentos vigentes: la Rev. C del manual
        de la bomba está reemplazada y tiene otros límites.
        """
        if self._buscador is None or self._hilo_buscador is None:
            raise FallaHerramienta("sin_buscador", "La búsqueda en manuales todavía no está disponible en este "
                                                   "sistema. Las otras tools andan.")
        if not getattr(self._buscador, "listo", True):
            raise FallaHerramienta("sin_buscador", "El índice de manuales todavía se está preparando: probá de nuevo "
                                                   "en un rato.", reintentable=True)
        crudos = self._buscar(entrada)
        if not isinstance(crudos, list):
            raise FallaHerramienta("error_buscador", "El buscador devolvió algo que no es una lista de fragmentos.")
        fragmentos = [_fragmento(c) for c in crudos[:entrada.k]]
        if entrada.solo_vigentes:
            fragmentos = [f for f in fragmentos if f.vigente]
        return SalidaBuscarManuales(fragmentos=fragmentos)

    # --- Lo que comparten las tools ---

    def _datos(self) -> dict:
        """GET /api/planta, leído una vez: no cambia mientras corre la planta. Si falla, se pide de nuevo la próxima."""
        with self._candado_planta:
            if self._datos_planta is None:
                datos = self._planta.planta()
                if not isinstance(datos, dict):
                    raise ValueError("/api/planta no devolvió un objeto")
                self._datos_planta = datos
            return self._datos_planta

    def _materiales(self) -> CatalogoMateriales:
        """El catálogo de materiales, leído una vez. Si no se pudo leer, se vuelve a probar la próxima."""
        with self._candado_catalogo:
            if self._catalogo is None:
                ruta = self._ruta_materiales
                try:
                    self._catalogo = CatalogoMateriales.leer(ruta)
                except FileNotFoundError:
                    raise FallaHerramienta("sin_datos", f"No está el catálogo de materiales ({ruta}).") from None
                except (OSError, yaml.YAMLError, ValidationError) as e:
                    raise FallaHerramienta("sin_datos", f"No se pudo leer el catálogo de materiales ({ruta}): "
                                                        f"{e}") from e
            return self._catalogo

    def _tag_de(self, nombre: str, datos: dict) -> str:
        """El TAG del equipo que nombra `nombre` ("la del medio" -> B-102), o no_encontrado."""
        tags = sorted({m.tag for m in Catalogo.desde_api(datos).menciones(nombre)})
        if len(tags) == 1:
            return tags[0]
        if tags:
            raise FallaHerramienta("no_encontrado", f"'{nombre}' nombra a más de un equipo: {', '.join(tags)}.")
        raise FallaHerramienta("no_encontrado", f"'{nombre}' no es el nombre de ningún equipo. 'La bomba' a secas no "
                                                "alcanza (son tres): hay que preguntar cuál. Sin filtros, la tool "
                                                "equipos trae todos los nombres.")

    def _sensor(self, tag: str, variable: str) -> Sensor:
        """El sensor (tag, variable) según /api/planta, o no_encontrado con lo que sí hay."""
        datos = self._datos()
        if tag not in {e.get("tag") for e in datos.get("equipos", [])}:
            raise FallaHerramienta("no_encontrado", self._no_existe(tag, datos))
        sensores = [_a_sensor(s) for s in datos.get("sensores", []) if s.get("tag") == tag]
        for sensor in sensores:
            if sensor.variable == variable:
                return sensor
        if not sensores:
            raise FallaHerramienta("no_encontrado", f"{tag} no tiene sensores conectados: sus lecturas son las de la "
                                                    "recorrida, que no quedan en el historian.")
        raise FallaHerramienta("no_encontrado", f"{tag} no tiene sensor de {variable}. Tiene: "
                                                f"{', '.join(s.variable for s in sensores)}.")

    @staticmethod
    def _no_existe(tag: str, datos: dict) -> str:
        tags = ", ".join(str(e.get("tag")) for e in datos.get("equipos", []))
        return f"No hay ningún equipo {tag}. Los equipos son: {tags}."

    def _buscar(self, entrada: EntradaBuscarManuales) -> Any:
        """Llama al buscador con timeout, y lo reintenta si falla (no si se pasa de tiempo: seguiría ocupado)."""
        assert self._buscador is not None and self._hilo_buscador is not None
        intentos = self.reintentos_buscador + 1
        for intento in range(intentos):
            futuro = self._hilo_buscador.submit(self._buscador.buscar, entrada.consulta, k=entrada.k,
                                                solo_vigentes=entrada.solo_vigentes, equipo=entrada.equipo)
            try:
                return futuro.result(timeout=self.timeout_buscador_s)
            except TimeoutError:
                futuro.cancel()
                raise FallaHerramienta("timeout", f"La búsqueda en manuales no terminó en {self.timeout_buscador_s:g} "
                                                  "s.", reintentable=True) from None
            except Exception as e:
                if intento + 1 == intentos:
                    raise FallaHerramienta("error_buscador", f"La búsqueda en manuales falló ({intentos} intentos): "
                                                             f"{e}", reintentable=True) from e
                log.warning("El buscador de manuales falló (%s); reintento %d", e, intento + 1)
                time.sleep(self.espera_buscador_s * (intento + 1))
        raise AssertionError("inalcanzable")

    def _error(self, tool: str, e: Exception) -> ErrorHerramienta:
        """Una excepción, como error para el modelo. Se registra corta si es esperable y con la traza si no."""
        intentos = f"{self._planta.reintentos + 1} intentos"
        if isinstance(e, FallaHerramienta):
            error = e.error
        elif isinstance(e, httpx.TimeoutException):
            error = ErrorHerramienta(tipo="timeout", reintentable=True,
                                     mensaje=f"La planta no contestó a tiempo ({intentos}).")
        elif isinstance(e, httpx.TransportError):
            error = ErrorHerramienta(tipo="sin_conexion", reintentable=True,
                                     mensaje=f"No se pudo conectar con la planta ({intentos}): {e}")
        elif isinstance(e, httpx.HTTPStatusError):
            codigo = e.response.status_code
            if codigo >= 500:
                error = ErrorHerramienta(tipo="error_planta", reintentable=True, estado_http=codigo,
                                         mensaje=f"La planta contestó con un error {codigo} ({intentos}).")
            else:
                error = ErrorHerramienta(tipo="pedido_rechazado", reintentable=False, estado_http=codigo,
                                         mensaje=f"La planta rechazó el pedido ({codigo}): {_detalle(e.response)}")
        elif isinstance(e, (ValueError, KeyError, TypeError)):
            log.warning("Tool %s: respuesta que no se entiende", tool, exc_info=True)
            return ErrorHerramienta(tipo="respuesta_invalida", reintentable=False,
                                    mensaje=f"La planta contestó algo que no se entiende ({type(e).__name__}: {e}).")
        else:
            log.exception("Tool %s: falla inesperada", tool)
            return ErrorHerramienta(tipo="error_interno", reintentable=False,
                                    mensaje="Falla interna del sistema; quedó registrada en el log.")
        nivel = logging.INFO if error.tipo in ("no_encontrado", "entrada_invalida") else logging.WARNING
        log.log(nivel, "Tool %s: %s: %s", tool, error.tipo, error.mensaje)
        return error


@dataclass(frozen=True)
class Definicion:
    """Una tool: su nombre (el del método de Herramientas), un título y sus modelos de entrada y salida."""
    nombre: str
    titulo: str
    entrada: type[Estricto]
    salida: type[Salida]

    @property
    def descripcion(self) -> str:
        """La que lee el modelo de lenguaje para decidir cuándo usarla: el docstring del método."""
        return inspect.getdoc(getattr(Herramientas, self.nombre)) or ""


DEFINICIONES = (
    Definicion("equipos", "Equipos de la planta", EntradaEquipos, SalidaEquipos),
    Definicion("historial_sensores", "Historial de un sensor", EntradaHistorialSensores, SalidaHistorialSensores),
    Definicion("historial_ordenes", "Historial de órdenes de trabajo", EntradaHistorialOrdenes, SalidaHistorialOrdenes),
    Definicion("materiales", "Repuestos, stock y lista de materiales", EntradaMateriales, SalidaMateriales),
    Definicion("catalogo_tareas", "Catálogo de tareas de mantenimiento", EntradaCatalogoTareas, SalidaCatalogoTareas),
    Definicion("buscar_manuales", "Búsqueda en manuales", EntradaBuscarManuales, SalidaBuscarManuales),
)
DEFINICION = {d.nombre: d for d in DEFINICIONES}


# --- Ayudantes -------------------------------------------------------------------------------------------------

def _en_hora_planta(ts: datetime) -> datetime:
    """La misma hora en el huso de la planta. Sin huso, se toma como hora de la planta."""
    return ts.replace(tzinfo=HUSO_PLANTA) if ts.tzinfo is None else ts.astimezone(HUSO_PLANTA)


def _lista(respuesta: Any, que: str) -> list:
    if not isinstance(respuesta, list):
        raise ValueError(f"{que} no devolvió una lista")
    return respuesta


def _detalle(respuesta: httpx.Response) -> str:
    """El porqué de un 4xx de la planta: el `detail` de FastAPI o el principio del texto."""
    try:
        cuerpo = respuesta.json()
    except ValueError:
        cuerpo = None
    detalle = cuerpo.get("detail") if isinstance(cuerpo, dict) else None
    if isinstance(detalle, list):      # el 422 de FastAPI: [{"loc": ["query", "desde"], "msg": "..."}]
        detalle = "; ".join(f"{'.'.join(map(str, d.get('loc', [])[1:]))}: {d.get('msg')}"
                            for d in detalle if isinstance(d, dict))
    return str(detalle or respuesta.text or respuesta.reason_phrase)[:300]


def _buscado(texto: str) -> str:
    buscado = normalizar(texto)
    if not buscado:
        raise FallaHerramienta("entrada_invalida", f"El texto a buscar ('{texto}') no tiene letras ni números.")
    return buscado


def _recortar(texto: str | None, largo: int = TEXTO_MAXIMO) -> str | None:
    if texto is None or len(texto) <= largo:
        return texto
    return texto[:largo - 1].rstrip() + "…"


def _a_sensor(s: dict) -> Sensor:
    return Sensor(variable=s["variable"], unidad=s.get("unidad"), alarma_alta=s.get("alarma_alta"),
                  alarma_baja=s.get("alarma_baja"), disparo=s.get("disparo"))


def _ficha(e: dict, sensores: list[dict]) -> FichaEquipo:
    return FichaEquipo(
        tag=e["tag"], tipo=e.get("tipo", ""), descripcion=e.get("descripcion"), fabricante=e.get("fabricante"),
        modelo=e.get("modelo"), nombres=list(e.get("nombres", [])),
        unidad_manometro=(e.get("manometro_local") or {}).get("unidad"), rol_inicial=e.get("rol_inicial"),
        caudal_nominal_m3d=e.get("caudal_nominal_m3d"), cartuchos=e.get("cartuchos"), altura_m=e.get("altura_m"),
        diametro_m=e.get("diametro_m"), sensores=[_a_sensor(s) for s in sensores if s.get("tag") == e["tag"]])


def _lecturas(crudas: list) -> list[tuple[datetime, float]]:
    """Las lecturas del historian con valor, en orden: [(hora, valor)]."""
    lecturas = []
    for m in crudas:
        valor = m.get("valor")
        if isinstance(valor, int | float) and not isinstance(valor, bool) and math.isfinite(valor):
            lecturas.append((datetime.fromisoformat(m["ts"]), float(valor)))
    return sorted(lecturas)


def _tendencia(valores: list[float]) -> Literal["subiendo", "bajando", "estable"]:
    """Como Historian.tendencia: el promedio del último cuarto contra el del primero, con 5 % de margen."""
    if len(valores) < 2:
        return "estable"
    cuarto = max(1, len(valores) // 4)
    inicio, fin = statistics.fmean(valores[:cuarto]), statistics.fmean(valores[-cuarto:])
    cambio = (fin - inicio) / abs(inicio) if inicio else fin - inicio
    return "subiendo" if cambio > 0.05 else "bajando" if cambio < -0.05 else "estable"


def _resumen(lecturas: list[tuple[datetime, float]], sensor: Sensor) -> ResumenSerie | None:
    if not lecturas:
        return None
    valores = [v for _, v in lecturas]
    ts_min, minimo = min(lecturas, key=lambda lectura: lectura[1])
    ts_max, maximo = max(lecturas, key=lambda lectura: lectura[1])
    alta, baja = sensor.alarma_alta, sensor.alarma_baja
    return ResumenSerie(
        lecturas=len(valores), minimo=minimo, ts_minimo=ts_min, maximo=maximo, ts_maximo=ts_max,
        promedio=round(statistics.fmean(valores), 3), primera=valores[0], ultima=valores[-1],
        tendencia=_tendencia(valores), sobre_alarma_alta=None if alta is None else sum(v > alta for v in valores),
        bajo_alarma_baja=None if baja is None else sum(v < baja for v in valores))


def _tramos(lecturas: list[tuple[datetime, float]], cuantos: int) -> list[Tramo]:
    """La serie partida en hasta `cuantos` tramos de la misma cantidad de lecturas."""
    if not lecturas:
        return []
    largo = math.ceil(len(lecturas) / cuantos)
    tramos = []
    for i in range(0, len(lecturas), largo):
        parte = lecturas[i:i + largo]
        valores = [v for _, v in parte]
        tramos.append(Tramo(desde=parte[0][0], hasta=parte[-1][0], promedio=round(statistics.fmean(valores), 3),
                            minimo=min(valores), maximo=max(valores)))
    return tramos


def _orden(o: Any) -> Orden | None:
    """Una orden de la API como Orden. Si viene mal armada se saltea (y se registra): no tira abajo el pedido."""
    try:
        return Orden(
            id=o["id"], fecha=o["fecha"], equipo=o.get("equipo") or "", tipo=o.get("tipo"), estado=o.get("estado"),
            origen=o.get("origen"), tareas=list(o.get("tareas") or []), ejecutor=o.get("ejecutor"),
            codigo_falla=o.get("codigo_falla"), aviso=_recortar(o.get("aviso")),
            trabajo_realizado=_recortar(o.get("trabajo_realizado")), descripcion=_recortar(o.get("descripcion")),
            repuestos=[RepuestoUsado(codigo=r["codigo"], cantidad=r["cantidad"]) for r in o.get("repuestos") or []],
            horas=o.get("horas"), fecha_inicio=o.get("fecha_inicio"), fecha_cierre=o.get("fecha_cierre"))
    except (KeyError, TypeError, AttributeError, ValidationError) as e:
        log.warning("Orden mal armada en el historial, se saltea: %s (%s)",
                    o.get("id") if isinstance(o, dict) else o, e)
        return None


def _tarea(t: _TareaCatalogo, estandar: bool, repuestos: dict[str, _RepuestoCatalogo]) -> Tarea:
    lleva = []
    for c in t.repuestos:
        r = repuestos.get(c.codigo)
        lleva.append(RepuestoDeTarea(codigo=c.codigo, nombre=r and r.nombre, cantidad=c.cantidad,
                                     costo_unitario_usd=r and r.costo_usd, stock=dict(r.stock) if r else {}))
    costo = sum(r.cantidad * r.costo_unitario_usd for r in lleva if r.costo_unitario_usd is not None)
    return Tarea(codigo=t.codigo, nombre=t.nombre, estandar=estandar, equipos=list(t.equipos), detiene=t.detiene,
                 horas=t.horas, ejecutor=t.ejecutor, repuestos=lleva, costo_repuestos_usd=costo)


def _fragmento(crudo: Any) -> Fragmento:
    """Un resultado del buscador como Fragmento; si le falta algo, error_buscador."""
    try:
        vigente = crudo["vigente"]
        return Fragmento(cita=str(crudo["cita"]), documento=str(crudo["documento"]), titulo=str(crudo["titulo"]),
                         texto=_recortar(str(crudo["texto"])), puntaje=round(float(crudo["puntaje"]), 4),
                         vigente=vigente if isinstance(vigente, str) else bool(vigente))
    except (KeyError, TypeError, ValueError) as e:
        raise FallaHerramienta("error_buscador", f"El buscador devolvió un fragmento mal armado ({e}).") from e

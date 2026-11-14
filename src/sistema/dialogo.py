"""El diálogo con el operador en la tablet: del texto de la nota a un parte confirmado y sus eventos.

Por cada nota:
1. Arma las filas del parte con el extractor.
2. Si dijo "la bomba" sin decir cuál, pregunta con opciones y sin filas (todavía no hay parte que mostrar).
3. Si hay lecturas con número, manda el borrador y espera. Si un diferencial vino dicho "dieciocho", lo
   anota como 1,8 y suma opciones para que el operador lo confirme. Si un valor está fuera de lo que marcó
   el equipo en los últimos 45 días, lo avisa en el mismo borrador, que de todos modos hay que confirmar.
4. Con cada respuesta: si confirma, guarda el parte; si corrige, aplica las correcciones y manda el
   borrador de nuevo, hasta MAX_BORRADORES.
5. Con el parte cerrado publica los eventos SinNovedad / Escalado.

Las notas sin lecturas con número ("la tres está parada, en reserva", "sin novedad") y las de condiciones
inseguras no tienen nada que confirmar: se le muestra al operador lo anotado y se cierran. El estado de cada
conversación queda en Postgres; si el sistema se reinicia, retoma las que esperaban respuesta.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from planta_contrato import MensajeTablet, NotaRecorrida, Pantalla, RespuestaOperador, Topicos
from pydantic import BaseModel

from .equipos import A_KG, Catalogo, FuenteDatosPlanta
from .eventos import SENSOR_DE, eventos_del_parte, motivos
from .extractor import DIFERENCIAL_ALTO, RODAMIENTO_CALIENTE, TIPO_DE, UNIDAD_FIJA, Fila, extraer
from .historian import Historian
from .llm import ExtractorLLM, a_filas
from .notas import NotaProcesada
from .persistencia import Almacen
from .reloj import RelojPlanta
from .respuestas import Correccion, interpretar

log = logging.getLogger(__name__)

MAX_BORRADORES = 4
ESPERA_MAX_S = 150.0       # segundos reales sin respuesta: se cierra el parte con lo que hay
MARGEN_RANGO = 0.05        # 5 % de tolerancia sobre el mínimo y el máximo de los últimos 45 días
ABIERTOS = ("pregunta", "borrador")

DICHO_VARIABLE = {"presion_descarga_local": "descarga", "presion_diferencial": "diferencial", "nivel": "nivel",
                  "temperatura_rodamiento_ir": "rodamiento", "temperatura_motor_ir": "motor",
                  "goteo_empaquetadura": "goteo"}
DICHO_UNIDAD = {"kg/cm2": "kilos", "psi": "psi", "bar": "bar", "m": "metros", "degC": "grados",
                "gotas/min": "gotas por minuto"}
TENDENCIA_DE = {"bomba": "vibracion", "filtro": "presion_diferencial"}


@dataclass
class Conversacion:
    nota: NotaRecorrida
    texto: str
    filas: list[Fila]
    seguridad: list[str] = field(default_factory=list)
    estado: str = "nueva"         # pregunta | borrador | confirmado | sin_confirmar | sin_respuesta | informado
    intento: int = 0              # borradores mandados
    preguntas: int = 0
    mensaje_id: str | None = None  # el último mensaje que espera respuesta
    enviado: float = 0.0           # time.monotonic() del último mensaje


def _numero(valor: float) -> str:
    return f"{valor:g}".replace(".", ",")


def _derivar(f: Fila) -> None:
    """Las observaciones que salen del valor: diferencial alto, rodamiento caliente."""
    if f.variable == "presion_diferencial" and f.valor is not None:
        if f.valor >= DIFERENCIAL_ALTO:
            f.observacion = "diferencial alto"
        elif f.observacion == "diferencial alto":
            f.observacion = None
    if f.variable == "temperatura_rodamiento_ir" and f.valor is not None:
        if f.valor > RODAMIENTO_CALIENTE:
            f.observacion = "rodamiento caliente"
        elif f.observacion == "rodamiento caliente":
            f.observacion = None


def _mas_cerca(filas: list[Fila], valor: float | None) -> Fila | None:
    if not filas:
        return None
    if valor is None:
        return filas[0]
    return min(filas, key=lambda f: abs((f.valor or 0) - valor))


def _variable_por_unidad(unidad: str | None, tipo: str) -> str:
    if unidad == "gotas/min":
        return "goteo_empaquetadura"
    if unidad == "degC":
        return "temperatura_rodamiento_ir"
    if unidad == "m" or tipo == "tanque":
        return "nivel"
    return "presion_diferencial" if tipo == "filtro" else "presion_descarga_local"


def aplicar(filas: list[Fila], correcciones: list[Correccion], catalogo: Catalogo) -> None:
    """Aplica al borrador lo que corrigió el operador."""
    for c in correcciones:
        numericas = [f for f in filas if f.valor is not None]
        if c.tipo == "valor" and c.tag and c.valor is not None:
            candidatas = [f for f in numericas if f.tag == c.tag]
            if c.unidad:
                # La unidad dice de qué lectura habla: "1610 psi" es la descarga aunque el borrador solo tuviera
                # la temperatura del rodamiento. Si esa lectura no estaba, se agrega y no se pisa la otra.
                variable = _variable_por_unidad(c.unidad, catalogo.equipos[c.tag].tipo)
                compatibles = [f for f in candidatas if f.variable == variable]
                if not compatibles:
                    nueva = next((f for f in filas if f.tag == c.tag and f.variable == variable), None)
                    if nueva is None:
                        nueva = Fila(c.tag, variable)
                        filas.append(nueva)
                    nueva.valor, nueva.unidad = c.valor, c.unidad
                    _derivar(nueva)
                    continue
                candidatas = compatibles
            f = _mas_cerca(candidatas, c.anterior)
            if f:
                f.valor, f.unidad = c.valor, c.unidad or f.unidad
                _derivar(f)
        elif c.tipo == "equipo" and c.tag:
            f = _mas_cerca([f for f in numericas if f.tag == c.tag_anterior] or numericas, c.valor)
            if f:
                f.tag = c.tag
                if f.variable == "presion_descarga_local":
                    f.unidad = catalogo.equipos[c.tag].unidad_manometro or f.unidad
        elif c.tipo == "unidad" and c.tag and c.unidad:
            for f in numericas:
                if f.tag == c.tag:
                    f.unidad = c.unidad
        elif c.tipo == "falta" and c.tag and c.valor is not None:
            tipo = catalogo.equipos[c.tag].tipo
            variable = _variable_por_unidad(c.unidad, tipo)
            unidad = c.unidad or UNIDAD_FIJA.get(variable) or catalogo.equipos[c.tag].unidad_manometro
            existente = next((f for f in filas if f.tag == c.tag and f.variable == variable), None)
            if existente:
                existente.valor, existente.unidad = c.valor, unidad
            else:
                existente = Fila(c.tag, variable, c.valor, unidad)
                filas.append(existente)
            _derivar(existente)
        elif c.tipo == "sobra" and c.tag:
            # Primero la que no puede ser de ese equipo (un diferencial en una bomba); si no, la más vieja:
            # las últimas las agregaron correcciones del mismo operador.
            candidatas = [f for f in numericas if f.tag == c.tag]
            tipo = catalogo.equipos[c.tag].tipo if c.tag in catalogo.equipos else None
            ajenas = [f for f in candidatas if TIPO_DE.get(f.variable, tipo) != tipo]
            if candidatas:
                filas.remove((ajenas or candidatas)[0])


def _combinar(reglas: list[Fila], modelo: list[Fila]) -> list[Fila]:
    """Las filas de las reglas más las que entendió el modelo y las reglas no."""
    vistas = {(f.tag, f.variable) for f in reglas}
    return reglas + [f for f in modelo if (f.tag, f.variable) not in vistas]


def _sin_novedad(texto: str) -> bool:
    t = texto.lower()
    return "sin novedad" in t or "todo normal" in t


class Dialogo:
    def __init__(self, bus, reloj: RelojPlanta, fuente: FuenteDatosPlanta, historian: Historian | None = None,
                 almacen: Almacen | None = None, llm: ExtractorLLM | None = None, modo: str = "mixto") -> None:
        self._bus = bus
        self._reloj = reloj
        self._fuente = fuente
        self._historian = historian
        self._almacen = almacen
        self._llm = llm
        self.modo = modo   # "reglas", "llm" o "mixto"
        self.conversaciones: dict[str, Conversacion] = {}
        self._por_mensaje: dict[str, str] = {}
        self._cola: queue.Queue[tuple[str, object] | None] = queue.Queue()
        self._hilo = threading.Thread(target=self._atender, name="dialogo", daemon=True)

    # ------------------------------------------------------------------ entrada

    def arrancar(self) -> None:
        self._hilo.start()

    def detener(self) -> None:
        self._cola.put(None)

    def nueva_nota(self, procesada: NotaProcesada) -> None:
        self._cola.put(("nota", procesada))

    def recibir_respuesta(self, respuesta: RespuestaOperador) -> None:
        self._cola.put(("respuesta", respuesta))

    def _atender(self) -> None:
        self.recuperar()
        while True:
            try:
                item = self._cola.get(timeout=5)
            except queue.Empty:
                self._vencidas()
                continue
            if item is None:
                return
            tipo, dato = item
            try:
                if tipo == "nota":
                    self.iniciar(dato)
                else:
                    self.responder(dato)
            except Exception:
                log.exception("Falló el diálogo (%s)", tipo)
            self._vencidas()

    # ------------------------------------------------------------------ nota nueva

    def iniciar(self, procesada: NotaProcesada) -> Conversacion:
        nota = procesada.nota
        datos = self._fuente.obtener()
        filas = extraer(procesada.texto, datos.catalogo, nota.fotos) if datos else []
        if datos and not procesada.hallazgos:
            filas = self._con_modelo(procesada.texto, filas, datos.catalogo)
        conv = Conversacion(nota, procesada.texto, filas, seguridad=[h.codigo for h in procesada.hallazgos])
        self.conversaciones[nota.id] = conv
        if conv.seguridad:
            # La alerta ya salió. El parte registra la condición; no se le pregunta nada al operador.
            equipo = datos.catalogo.unico(procesada.hallazgos[0].oracion) if datos else None
            conv.filas = [Fila(equipo or "PLANTA", "condicion_insegura", observacion=procesada.texto,
                               foto=nota.fotos[0] if nota.fotos else None)] + [f for f in filas if not f.ambigua]
            self._informar(conv, "Condición insegura registrada")
        elif nota.id.startswith("VIVO"):
            conv.filas = [f for f in filas if not f.ambigua]   # nota en vivo: no tiene parte esperado
            self._informar(conv)
        elif any(f.ambigua == "equipo" for f in conv.filas):
            self._preguntar_equipo(conv)
        else:
            self._seguir(conv)
        return conv

    def _con_modelo(self, texto: str, reglas: list[Fila], catalogo: Catalogo) -> list[Fila]:
        """En modo "mixto" el modelo de lenguaje entra solo cuando las reglas no sacaron ninguna fila de una
        nota que dice algo; en modo "llm", siempre (y las reglas quedan de respaldo)."""
        if self._llm is None or self.modo == "reglas":
            return reglas
        if self.modo == "mixto" and (reglas or _sin_novedad(texto) or len(texto.split()) < 4):
            return reglas
        filas_modelo = self._llm.extraer(texto)
        if filas_modelo is None:
            return reglas
        filas = a_filas(filas_modelo, catalogo)
        if self.modo == "llm":
            fotos = {(f.tag, f.variable): f.foto for f in reglas if f.foto}
            for f in filas:
                f.foto = fotos.get((f.tag, f.variable))
            return filas
        return _combinar(reglas, filas)

    def _seguir(self, conv: Conversacion) -> None:
        if any(f.valor is not None for f in conv.filas):
            self._mandar_borrador(conv)
        else:
            self._informar(conv)

    # ------------------------------------------------------------------ mensajes a la tablet

    def _decir(self, f: Fila, catalogo: Catalogo | None) -> str:
        nombre = catalogo.nombre(f.tag) if catalogo and f.tag else (f.tag or "?")
        if f.valor is None:
            return f"{nombre}: {f.observacion or f.variable}"
        texto = f"{nombre}, {DICHO_VARIABLE.get(f.variable, f.variable)} {_numero(f.valor)}"
        texto += f" {DICHO_UNIDAD.get(f.unidad, f.unidad)}" if f.unidad else ""
        return texto + (f" ({f.observacion})" if f.observacion else "")

    def _fuera_de_rango(self, conv: Conversacion, catalogo: Catalogo) -> list[str]:
        avisos = []
        if self._historian is None:
            return avisos
        for f in conv.filas:
            sensor = SENSOR_DE.get(f.variable)
            if not sensor or f.valor is None or not f.tag:
                continue
            rango = self._historian.rango(f.tag, sensor, self._reloj.ahora())
            if rango is None:
                continue
            kg = f.valor * A_KG.get(f.unidad or "kg/cm2", 1.0)
            minimo, maximo = rango
            if kg < minimo * (1 - MARGEN_RANGO) or kg > maximo * (1 + MARGEN_RANGO):
                avisos.append(f"{_numero(f.valor)} {DICHO_UNIDAD.get(f.unidad, f.unidad or '')} en "
                              f"{catalogo.nombre(f.tag)} está fuera de lo que marcó en los últimos 45 días "
                              f"({_numero(round(minimo, 1))} a {_numero(round(maximo, 1))} kilos)")
        return avisos

    def _mandar_borrador(self, conv: Conversacion) -> None:
        datos = self._fuente.obtener()
        catalogo = datos.catalogo if datos else None
        conv.intento += 1
        dudosas = [f for f in conv.filas if f.ambigua == "valor"]
        texto = "Te leo lo que entendí: " + "; ".join(self._decir(f, catalogo) for f in conv.filas) + "."
        for f in dudosas:
            texto += f" Dijiste {f.dicho} de diferencial: lo anoté como {_numero(f.valor)}."
        for aviso in self._fuera_de_rango(conv, catalogo) if catalogo else []:
            texto += f" Ojo: {aviso}."
        texto += " ¿Está bien?"
        mensaje = MensajeTablet(
            id=f"TAB-{conv.nota.id}-B{conv.intento}", ts=self._reloj.ahora(), nota_ids=[conv.nota.id], texto=texto,
            pantalla=Pantalla(titulo=f"Parte de recorrida · borrador {conv.intento}",
                              filas=[f.contrato() for f in conv.filas],
                              opciones=["Sí, es así", "No, está mal"] if dudosas else []),
            espera_respuesta=True)
        self._esperar(conv, mensaje, "borrador")

    def _preguntar_equipo(self, conv: Conversacion) -> None:
        catalogo = self._fuente.obtener().catalogo
        ambigua = next(f for f in conv.filas if f.ambigua == "equipo")
        candidatos = catalogo.de_tipo(TIPO_DE.get(ambigua.variable, "bomba")) or list(catalogo.equipos)
        if ambigua.unidad:   # primero el equipo cuyo manómetro está en la unidad que dijo
            candidatos.sort(key=lambda t: catalogo.equipos[t].unidad_manometro != ambigua.unidad)
        conv.preguntas += 1
        mensaje = MensajeTablet(
            id=f"TAB-{conv.nota.id}-P{conv.preguntas}", ts=self._reloj.ahora(), nota_ids=[conv.nota.id],
            texto=f"Dijiste «{ambigua.dicho or 'el equipo'}» sin decir cuál. ¿Cuál es?",
            pantalla=Pantalla(titulo="¿Qué equipo?",
                              opciones=[f"{catalogo.nombre(t).capitalize()} ({t})" for t in candidatos]),
            espera_respuesta=True)
        self._esperar(conv, mensaje, "pregunta")

    def _informar(self, conv: Conversacion, titulo: str = "Parte de recorrida") -> None:
        """Muestra lo anotado sin esperar respuesta (no hay lecturas que confirmar) y cierra el parte."""
        if not conv.filas:
            conv.filas = [Fila("PLANTA", "recorrida", observacion="sin novedad")]
        datos = self._fuente.obtener()
        catalogo = datos.catalogo if datos else None
        mensaje = MensajeTablet(
            id=f"TAB-{conv.nota.id}-INFO", ts=self._reloj.ahora(), nota_ids=[conv.nota.id],
            texto="Anoté: " + "; ".join(self._decir(f, catalogo) for f in conv.filas) + ".",
            pantalla=Pantalla(titulo=titulo, filas=[f.contrato() for f in conv.filas]), espera_respuesta=False)
        self._publicar(Topicos.TABLET, mensaje)
        self._cerrar(conv, "informado")

    def _esperar(self, conv: Conversacion, mensaje: MensajeTablet, estado: str) -> None:
        if conv.mensaje_id:
            self._por_mensaje.pop(conv.mensaje_id, None)
        conv.estado, conv.mensaje_id, conv.enviado = estado, mensaje.id, time.monotonic()
        self._por_mensaje[mensaje.id] = conv.nota.id
        self._guardar(conv)
        self._publicar(Topicos.TABLET, mensaje)

    # ------------------------------------------------------------------ respuestas

    def responder(self, respuesta: RespuestaOperador) -> None:
        nota_id = self._por_mensaje.get(respuesta.mensaje_id)
        conv = self.conversaciones.get(nota_id) if nota_id else None
        if conv is None or conv.mensaje_id != respuesta.mensaje_id or conv.estado not in ABIERTOS:
            return
        catalogo = self._fuente.obtener().catalogo
        r = interpretar(respuesta.texto, catalogo)
        log.info("%s: el operador contestó %r (%s)", conv.nota.id, respuesta.texto, r.tipo)
        if conv.estado == "pregunta":
            tag = r.tag if r.tipo == "elige" else None
            for f in conv.filas:
                if f.ambigua == "equipo":
                    if tag is None:
                        continue
                    f.tag, f.ambigua = tag, None
                    if f.variable == "presion_descarga_local" and f.unidad is None:
                        f.unidad = catalogo.equipos[tag].unidad_manometro
            conv.filas = [f for f in conv.filas if f.ambigua != "equipo"]   # si no se entendió, se descartan
            self._seguir(conv)
            return
        for f in conv.filas:
            f.ambigua = None   # el operador ya revisó el borrador
        if r.tipo == "confirma":
            self._confirmar(conv)
        elif r.tipo == "corrige" and conv.intento < MAX_BORRADORES:
            antes = [f.como_dict() for f in conv.filas]
            aplicar(conv.filas, r.correcciones, catalogo)
            if [f.como_dict() for f in conv.filas] == antes:
                # Mandar el mismo borrador otra vez solo haría repetir la misma corrección.
                log.warning("%s: no se pudo aplicar la corrección; el parte queda sin confirmar", conv.nota.id)
                self._cerrar(conv, "sin_confirmar")
            else:
                self._mandar_borrador(conv)
        else:
            self._cerrar(conv, "sin_confirmar")

    def _confirmar(self, conv: Conversacion) -> None:
        catalogo = self._fuente.obtener().catalogo
        mensaje = MensajeTablet(
            id=f"TAB-{conv.nota.id}-OK", ts=self._reloj.ahora(), nota_ids=[conv.nota.id],
            texto="Listo, guardé el parte.",
            pantalla=Pantalla(titulo="Parte confirmado", filas=[f.contrato() for f in conv.filas]),
            espera_respuesta=False)
        self._publicar(Topicos.TABLET, mensaje)
        self._cerrar(conv, "confirmado")
        log.info("%s: parte confirmado al borrador %d: %s", conv.nota.id, conv.intento,
                 "; ".join(self._decir(f, catalogo) for f in conv.filas))

    # ------------------------------------------------------------------ cierre y eventos

    def _contexto(self, conv: Conversacion, catalogo: Catalogo, por_equipo: dict[str, list[str]]) -> dict[str, str]:
        """Lo que acompaña a cada escalado: las fotos del equipo y la tendencia de las últimas horas."""
        contexto = {}
        for tag, lista in por_equipo.items():
            if not lista:
                continue
            partes = []
            fotos = [f.foto for f in conv.filas if f.tag == tag and f.foto]
            if fotos:
                partes.append("Fotos: " + ", ".join(fotos))
            variable = TENDENCIA_DE.get(catalogo.equipos[tag].tipo) if tag in catalogo.equipos else None
            if variable and self._historian:
                tendencia = self._historian.tendencia(tag, variable, self._reloj.ahora())
                if tendencia:
                    partes.append(tendencia.capitalize())
            contexto[tag] = ". ".join(partes)
        return contexto

    def _cerrar(self, conv: Conversacion, estado: str) -> None:
        conv.estado = estado
        if conv.mensaje_id:
            self._por_mensaje.pop(conv.mensaje_id, None)
        self._guardar(conv)
        if conv.seguridad:
            return   # el evento de esta nota es la AlertaSeguridad, que ya salió
        datos = self._fuente.obtener()
        limites = datos.limites if datos else {}
        por_equipo = motivos(conv.filas, limites)
        contexto = self._contexto(conv, datos.catalogo, por_equipo) if datos else {}
        for evento in eventos_del_parte(conv.nota.id, conv.filas, limites, self._reloj.ahora(), contexto):
            self._publicar(Topicos.EVENTOS, evento)
            log.info("%s: evento %s %s", conv.nota.id, evento.tipo.value, evento.equipo or "")

    def _vencidas(self) -> None:
        ahora = time.monotonic()
        for conv in list(self.conversaciones.values()):
            if conv.estado in ABIERTOS and ahora - conv.enviado > ESPERA_MAX_S:
                log.warning("%s: sin respuesta del operador; se cierra con lo que hay", conv.nota.id)
                conv.filas = [f for f in conv.filas if f.ambigua != "equipo"]
                self._cerrar(conv, "sin_respuesta")

    # ------------------------------------------------------------------ persistencia

    def _guardar(self, conv: Conversacion) -> None:
        if self._almacen:
            self._almacen.guardar_parte(conv.nota, conv.texto, conv.estado, conv.intento, conv.preguntas,
                                        conv.mensaje_id, [f.como_dict() for f in conv.filas], conv.seguridad)

    def recuperar(self) -> None:
        """Retoma las conversaciones que esperaban respuesta cuando se apagó el sistema."""
        if not self._almacen:
            return
        for p in self._almacen.partes_abiertas():
            conv = Conversacion(NotaRecorrida.model_validate(p["nota"]), p["texto"], [Fila(**f) for f in p["filas"]],
                                p["seguridad"], p["estado"], p["intento"], p["preguntas"], p["mensaje_id"],
                                time.monotonic())
            self.conversaciones[conv.nota.id] = conv
            if conv.mensaje_id:
                self._por_mensaje[conv.mensaje_id] = conv.nota.id
        if self.conversaciones:
            log.info("Retomadas %d conversaciones abiertas", len(self.conversaciones))

    def _publicar(self, topico: str, mensaje: BaseModel, intentos: int = 5) -> bool:
        for _ in range(intentos):
            if self._bus.publicar(topico, mensaje):
                if self._almacen:
                    self._almacen.guardar("salida", topico, mensaje.model_dump(mode="json"))
                return True
            time.sleep(1.0)
        log.error("No se pudo publicar en %s", topico)
        return False

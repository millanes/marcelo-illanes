"""La API de la planta y el buscador de manuales en memoria, para probar las tools sin red."""

import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from conftest import PLANTA

from sistema.herramientas import ClienteConsultas, Herramientas

MATERIALES = Path(__file__).resolve().parents[1] / "datos" / "materiales.yaml"

# GET /api/planta: el de conftest más lo que usan las tools.
PLANTA_API = {
    "planta": {"nombre": "Planta de Inyección PIA-3", "inyeccion_programada_m3d": 2400},
    "equipos": PLANTA["equipos"],
    "sensores": [*PLANTA["sensores"],
                 {"tag": "B-101", "variable": "vibracion", "unidad": "mm/s", "alarma_alta": 7.1, "disparo": 11.2}],
}

# GET /api/historial-ordenes: como la real, del archivo (con todos los campos) y del simulador (con menos).
ORDENES = [
    {"id": "OT-24-0001", "fecha": "2024-10-03T20:15-03:00", "fecha_aviso": "2024-10-03T20:15-03:00",
     "fecha_inicio": "2024-10-04T01:15-03:00", "fecha_cierre": "2024-10-04T07:15-03:00", "equipo": "B-101",
     "tipo": "correctiva", "tareas": ["T-B-VAL"], "ejecutor": "propio", "solicitante": "Rubén Catriel",
     "tecnico": "Ramón Cayupán", "aviso": "pulsacion alta en descraga de bomba 1",
     "trabajo_realizado": "valvulas gastadas, un resorte partido en descarga 2. se cambio kit completo",
     "repuestos": [{"codigo": "TX-VAL-KIT", "cantidad": 1}], "horas": 6.0, "codigo_falla": None,
     "estado": "Cerrada", "origen": "archivo"},
    {"id": "OT-26-0069", "fecha": "2026-08-04T15:50-03:00", "equipo": "B-103", "tipo": "correctiva",
     "tareas": ["T-B-EMP"], "ejecutor": "propio", "aviso": "LA DEL FONDO PIERDE OTRA VEZ X EL 3",
     "trabajo_realizado": "EMPAQUETADURA DE NUEVO A LAS 6 SEMANAS. EMBOLO 3 RAYADO",
     "repuestos": [{"codigo": "TX-EMP-KIT", "cantidad": 1}], "horas": 3.5, "codigo_falla": "F01",
     "estado": "Cerrada", "origen": "archivo"},
    {"id": "OT-26-0076", "fecha": "2026-08-22T10:40-03:00", "equipo": "B-103", "tipo": "correctiva",
     "tareas": ["T-B-EMP"], "ejecutor": "propio", "aviso": "bba 3 empaquetadura pierde, charco abajo del cabezal",
     "trabajo_realizado": "EMPAQUETADURA NUEVA. OK SIN PERDIDA", "repuestos": [{"codigo": "TX-EMP-KIT", "cantidad": 1}],
     "horas": 4.5, "codigo_falla": None, "estado": "Cerrada", "origen": "archivo"},
    {"id": "OT-H-0003", "fecha": "2026-11-08T19:09:00-03:00", "equipo": "B-102", "tareas": ["T-B-EMP"],
     "ejecutor": "propio", "descripcion": "Corrección de falla F01", "estado": "Cerrada", "origen": "simulador"},
]


class PlantaFalsa:
    """Contesta como la API de la planta. `fallar(ruta, ...)` encola fallas para las rutas que empiezan así: un
    código HTTP o una excepción de httpx, que se consumen de a una antes de volver a contestar bien."""

    def __init__(self, ahora: str = "2026-11-12T14:00:30.250-03:00") -> None:
        self.ahora = ahora
        self.ordenes = list(ORDENES)
        self.historian: list | dict | None = None      # si no es None, lo que contesta el historian
        self.pedidos: list[httpx.Request] = []
        self._fallas: dict[str, list] = {}

    def fallar(self, ruta: str, *fallas) -> None:
        self._fallas.setdefault(ruta, []).extend(fallas)

    def rutas(self) -> list[str]:
        return [p.url.path for p in self.pedidos]

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        ruta = pedido.url.path
        for prefijo, fallas in self._fallas.items():
            if ruta.startswith(prefijo) and fallas:
                falla = fallas.pop(0)
                if isinstance(falla, int):
                    return httpx.Response(falla, json={"detail": "falla de prueba"})
                raise falla("falla de prueba", request=pedido)
        if ruta == "/api/planta":
            return httpx.Response(200, json=PLANTA_API)
        if ruta == "/api/estado":
            return httpx.Response(200, json={"ts": self.ahora, "fallas": [{"codigo": "F01", "equipo": "B-102"}]})
        if ruta.startswith("/api/historian/"):
            return httpx.Response(200, json=self.historian if self.historian is not None else self._serie(pedido))
        if ruta == "/api/historial-ordenes":
            return httpx.Response(200, json=self._ordenes(pedido))
        return httpx.Response(404, json={"detail": "Not Found"})

    @staticmethod
    def _serie(pedido: httpx.Request) -> list[dict]:
        """Una lectura por minuto entre desde y hasta: sube de a 0,01 desde 2, con un minuto sin dato (el 5) y un
        pico de 8 en el minuto 100."""
        desde = datetime.fromisoformat(pedido.url.params["desde"])
        hasta = datetime.fromisoformat(pedido.url.params["hasta"])
        ts = desde.replace(second=0, microsecond=0)
        ts += timedelta(minutes=1) if ts < desde else timedelta()
        serie, i = [], 0
        while ts <= hasta:
            valor = None if i == 5 else 8.0 if i == 100 else round(2.0 + 0.01 * i, 2)
            serie.append({"ts": ts.isoformat(), "valor": valor})
            ts, i = ts + timedelta(minutes=1), i + 1
        return serie

    def _ordenes(self, pedido: httpx.Request) -> list[dict]:
        p = pedido.url.params
        desde = datetime.fromisoformat(p["desde"]) if "desde" in p else None
        hasta = datetime.fromisoformat(p["hasta"]) if "hasta" in p else None
        return [o for o in self.ordenes
                if (not p.get("equipo") or o.get("equipo") == p["equipo"])
                and (desde is None or datetime.fromisoformat(o["fecha"]) >= desde)
                and (hasta is None or datetime.fromisoformat(o["fecha"]) <= hasta)]


def fragmento(cita: str, vigente: bool = True, texto: str = "Vibración: alarma 7,1 mm/s, disparo 11,2 mm/s.",
              puntaje: float = 0.9) -> dict:
    documento = cita.split(" §")[0]
    return {"cita": cita, "documento": documento, "titulo": f"Sección {cita}", "texto": texto, "vigente": vigente,
            "puntaje": puntaje, "fabricante": "Trimax Industrial S.A."}


class BuscadorFalso:
    """Un buscador de manuales que contesta `resultados`; falla las primeras `fallas` veces y, con `trabar`, se
    queda esperando hasta que se suelte."""

    def __init__(self, resultados: list | None = None, fallas: int = 0, trabar: bool = False) -> None:
        self.resultados = resultados if resultados is not None else [fragmento("TX350-D §3.2")]
        self.fallas, self.trabar = fallas, trabar
        self.soltar = threading.Event()
        self.llamados: list[tuple] = []

    def buscar(self, consulta: str, k: int = 5, solo_vigentes: bool = True, equipo: str | None = None) -> list[dict]:
        self.llamados.append((consulta, k, solo_vigentes, equipo))
        if self.fallas:
            self.fallas -= 1
            raise RuntimeError("se cortó la conexión con la base")
        if self.trabar:
            self.soltar.wait(5)
        return self.resultados


def herramientas(planta: PlantaFalsa, buscador=None, materiales: Path = MATERIALES, **opciones) -> Herramientas:
    """Las tools sobre la planta falsa, sin esperas entre reintentos."""
    cliente = ClienteConsultas("http://sim:8100", espera_s=0, transporte=httpx.MockTransport(planta))
    return Herramientas(cliente, materiales, buscador=buscador, espera_buscador_s=0, **opciones)

# Sistema de planta · marcelo-illanes

TP de la Academia Novit 2026, opción A: planta de inyección de agua.

Este repo tiene el sistema que corre al lado de la planta simulada ([academia2026-tp](https://github.com/NovitSoftware/academia2026-tp)). Escucha lo que publica la planta por MQTT (sensores, notas de voz de la recorrida, respuestas del operador) y le contesta en los tópicos `sistema/...`.

## Estado

| Parte | Qué | Estado |
|---|---|---|
| Base | Conexión con la planta, latido cada 30 s, Postgres con pgvector | Hecho |
| A · Parte por voz | Transcripción local de cada nota | Hecho |
| A · Parte por voz | Borrador en la tablet, preguntas por ambigüedad, correcciones del operador, Borrador → Confirmado | Hecho, con reglas |
| A · Parte por voz | Extracción con modelo de lenguaje (LangChain, structured output) para lo que las reglas no entienden | Pendiente |
| B · Detección | Alerta de seguridad (S1 a S4) en la tablet y como evento, sin nube | Hecho |
| B · Detección | `SinNovedad` / `Escalado` por equipo de cada parte, en código | Hecho |
| B · Detección | Desvíos sostenidos de `planta/sensores/#` | Pendiente |
| Tools y MCP | Herramientas de solo lectura con Pydantic estricto, al menos una por MCP | Pendiente |
| RAG y memoria | Manuales en pgvector con recall@k medido, estado de cada nota en Postgres | Pendiente |

## Cómo levantarlo

Hace falta Docker con Compose y la planta levantada, porque la planta crea la red `planta-lan`.

1. En el repo de la planta (`academia2026-tp`):

   ```bash
   docker compose up -d --build
   ```

2. En este repo (la primera vez baja el modelo de transcripción, unos 500 MB):

   ```bash
   docker compose up -d --build
   ```

3. Para ver qué hace:

   ```bash
   docker compose logs -f sistema
   ```

   Cada nota aparece con su transcripción y lo que tardó. El latido y las alertas se ven en el visor de la planta: http://localhost:8081/?instructor

Para apagarlo, `docker compose down`. Con `-v` también borra la base.

No hay que correr al mismo tiempo el sistema de ejemplo de la planta (`planta_sim.ejemplo_sistema`): los dos le contestarían a la misma tablet.

## Tests

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

En Linux o macOS, `.venv/bin/python` en lugar de `.venv/Scripts/python`.

## Corrección automática

Última corrida (12/11, `martes-tranquilo` y `fuga-sin-enlace`): **88,9 / 100, aprueba**. A1, A4a, A4b, B1, B2, F1 y F2 al 100 %. A2 y A3 al 69 %: las notas que no se confirman son las que no tienen ninguna lectura con número ("la bomba está parada, en reserva" y la nota de la fuga), que el operador virtual no puede confirmar porque solo compara valores. Las 10 notas con lecturas se confirmaron al primer borrador.

Desde el repo de la planta, con este repo al lado:

```bash
python correccion/corregir.py --repo ../marcelo-illanes --escenarios martes-tranquilo fuga-sin-enlace
```

Deja el informe en `correccion/corridas/marcelo-illanes/informe.md` del repo de la planta.

## Cómo está armado

```text
docker-compose.yml   sistema y postgres, en planta-lan, con el label academia.planta: pc y el proxy de la planta
Dockerfile           imagen del sistema (Python 3.12) con el modelo de transcripción adentro
src/sistema/
  __main__.py        arranque
  config.py          configuración por variables de entorno
  bus.py             MQTT: suscripción a planta/... y publicación de mensajes del contrato
  despacho.py        qué se hace con cada mensaje: sensores en el hilo de red, el resto en una cola
  notas.py           cada nota de voz: bajar el audio, transcribir, buscar condiciones inseguras, alertar
  transcripcion.py   Whisper local (faster-whisper en CPU)
  seguridad.py       detector de condiciones inseguras S1 a S4, con reglas
  extractor.py       del texto de la nota a las filas del parte (FilaParte), con reglas
  numeros.py         números dichos en campo: "mil seiscientos cuarenta", "uno con cuatro", "09"
  dialogo.py         la conversación con el operador: borrador, preguntas, correcciones, confirmación
  respuestas.py      lo que contesta el operador ("No, la uno estaba en ciento quince kilos")
  eventos.py         SinNovedad / Escalado por equipo, con los límites de /api/planta
  historian.py       rango de 45 días y tendencia de las últimas horas (GET /api/historian)
  equipos.py         equipos, nombres informales y límites de alarma, sacados de GET /api/planta
  planta.py          cliente HTTP de la planta, con timeouts y reintentos
  texto.py           normalización de texto hablado
  latido.py          sistema/estado cada 30 s
  enlace.py          prueba si hay enlace a internet a través del proxy
  reloj.py           hora de la planta: la de sus mensajes, no la de la máquina
  persistencia.py    Postgres con pgvector: mensajes y notas con su transcripción
tests/               tests de cada pieza
```

### Decisiones

- **El latido depende solo del broker.** No espera a la base ni a los modelos: en la corrección el reloj de la planta arranca con el primer latido, y un hueco de más de 60 s cuenta en contra.
- **La hora es la de la planta.** El simulador corre acelerado y con fecha propia, así que los mensajes salen con la hora de las últimas lecturas de sensores.
- **Todo mensaje que sale es un modelo de `planta_contrato`.** Si no cumple el contrato, falla al armarlo y no llega a publicarse.
- **Sesión MQTT limpia.** Al arrancar no se reciben mensajes viejos que el broker hubiera guardado para este cliente.
- **El enlace se prueba a través del proxy.** Un pedido corto a la nube de NVIDIA: cualquier respuesta HTTP quiere decir que hay enlace.
- **Las notas pasan por un carril rápido.** Una por vez y en orden de llegada: bajar el audio, transcribir y buscar condiciones inseguras. La alerta sale antes de guardar nada y antes del borrador del parte, porque el corrector da 30 s reales desde que la planta publica la nota.
- **La alerta de seguridad es en código, sin modelos.** Reglas por condición que piden algo específico ("agua a presión", no solo "agua"), descartan lo negado ("no hay olor a gas", "nadie se lastimó") y, cuando la señal es ambigua ("se cortó" puede ser la luz), piden una persona o una parte del cuerpo en la misma oración. Las notas normales de la planta ("chorrea por la empaquetadura", "está rebalsando a la pileta") están en los tests como casos que no tienen que alertar.
- **Una alerta por nota.** El evento `EV-<nota>-SEG` y el aviso `TAB-<nota>-SEG` tienen id fijo: si la nota llega dos veces, no se duplican.
- **El parte se arma con reglas.** Para cada palabra ancla ("descarga", "diferencial", "metros", "grados", "empaquetadura", "parada"...) toma el equipo nombrado antes y el número dicho justo antes. No depende de la puntuación, que Whisper pone donde quiere, y tolera sus errores conocidos ("medio" por "me dio", "09" por "cero nueve", "golpe teometálico"). Las unidades salen de lo dicho o del manómetro de cada bomba (kg/cm² la B-101, psi la B-102, bar la B-103).
- **Pregunta solo cuando hace falta.** "La bomba" a secas: pregunta cuál con opciones, antes de mostrar el borrador. Un diferencial dicho "dieciocho": lo anota como 1,8 y lo muestra con opciones para confirmar. Un valor fuera de lo que marcó el equipo en los últimos 45 días se avisa en el mismo borrador, que de todos modos se confirma. Las notas claras van directo al borrador, sin preguntas.
- **Las correcciones del operador se aplican.** Valor, equipo, unidad, lectura que faltó o que sobra; después vuelve a mandar el borrador, hasta 4 veces. Las notas sin lecturas con número (por ejemplo, "la tres está parada, en reserva") se muestran sin pedir confirmación.
- **Los eventos salen del parte cerrado.** Un evento por equipo: `Escalado` si el parte dice algo anormal (cualquier observación salvo "parada, en reserva" o "parada por mantenimiento") o si una lectura pasa un límite de alarma de `/api/planta`; si no, `SinNovedad`. El escalado lleva las fotos del equipo y la tendencia de las últimas 6 horas. Ids fijos por nota y equipo (`EV-<nota>-<tag>`).
- **La conversación de cada nota queda en Postgres** (tabla `partes`: estado, intento, último mensaje, filas). Si el sistema se reinicia, retoma las que esperaban respuesta. Si el operador no contesta en 150 s reales, el parte se cierra con lo que hay.
- **Recursos.** Entre todos los servicios de planta, como mucho 4 CPU y 8 GB. El sistema (con la transcripción) tiene 2 CPU y 2 GB, y Postgres 0,5 CPU y 512 MB. Quedan 1,5 CPU y 5,5 GB para el modelo de lenguaje.

### Transcripción

Provisoria hasta tener la lista cerrada de modelos del curso: Whisper small con [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2, int8, CPU), con el idioma fijo en castellano, filtro de voz (VAD) y un prompt con el vocabulario de la planta. Cambiar de modelo es cambiar `STT_MODELO` en el Dockerfile.

Medido sobre 10 notas reales de la planta (5 a 20 s de audio), con 2 CPU:

| Modelo | Tiempo por nota | Peor nota | Errores por palabra |
|---|---|---|---|
| Whisper base, beam 1 | 1,5 s | 2,3 s | 33 % |
| **Whisper small, beam 1** | **4,4 s** | **6,2 s** | **25 %** |
| Whisper small, beam 5 | 6,0 s | 9,3 s | 27 % |

El error por palabra exagera: Whisper escribe los números con cifras ("1630") y el texto de referencia con letras ("mil seiscientos treinta"). Los errores que importan son otros, como "medio" por "me dio" o "la huma" por "la bomba" (este último solo con base).

Con la planta corriendo al lado, la alerta salió a los 7,2 s de llegar la nota en una fuga S1 (10 s de audio) y a los 5,4 s en un olor a gas S2 (7 s de audio). Las dos transcripciones salieron sin errores. El corrector da 30 s.

## Qué corre en la planta y qué en la nube

Se completa cuando estén elegidos todos los modelos. Por ahora todo corre en la planta: la transcripción y la detección de condiciones inseguras tienen que andar sin enlace.

## Contrato

`planta_contrato` se instala con pip desde el repo público de la planta, fijado al commit `ab65b95` (ver `pyproject.toml`). El link de `docs/contrato.md` apunta a `academia2026-planta`, que no existe.

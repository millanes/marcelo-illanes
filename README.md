# Sistema de planta · marcelo-illanes

TP de la Academia Novit 2026, opción A: planta de inyección de agua.

Este repo tiene el sistema que corre al lado de la planta simulada ([academia2026-tp](https://github.com/NovitSoftware/academia2026-tp)). Escucha lo que publica la planta por MQTT (sensores, notas de voz de la recorrida, respuestas del operador) y le contesta en los tópicos `sistema/...`. Todo corre en la PC de la planta, sin nube.

## Estado (Entrega 1)

| Parte | Qué | Estado |
|---|---|---|
| Base | Conexión con la planta, latido cada 30 s, Postgres 16 con pgvector | Hecho |
| A · Parte por voz | Transcripción local de cada nota (Whisper small, CPU) | Hecho |
| A · Parte por voz | Borrador en la tablet, preguntas por ambigüedad, correcciones del operador, Borrador → Confirmado | Hecho |
| A · Parte por voz | Extracción con modelo de lenguaje local (LangChain, structured output) | Hecho, como respaldo de las reglas |
| B · Detección | Alerta de seguridad (S1 a S4) en la tablet y como evento, sin nube | Hecho |
| B · Detección | `SinNovedad` / `Escalado` por equipo de cada parte, en código | Hecho |
| B · Detección | Desvíos sostenidos de `planta/sensores/#` (origen `sensor`) | Hecho |
| Tools y MCP | Seis tools de solo lectura con Pydantic estricto, expuestas por MCP | Hecho |
| Control y evaluación | Condiciones de corte y presupuestos, mini spec, evaluación contra `/api/verdad` | Hecho |
| RAG, contexto y memoria | Manuales en pgvector con recall@k medido contra BM25, constructor de contexto diseñado, política de memoria | Hecho |

## Cómo levantarlo

Hace falta Docker con Compose y la planta levantada, porque la planta crea la red `planta-lan`.

1. En el repo de la planta (`academia2026-tp`):

   ```bash
   docker compose up -d --build
   ```

2. En este repo. La primera vez baja los modelos y los deja adentro de las imágenes: Whisper small (unos 500 MB) e imagen de Ollama 0.33.2 con `qwen2.5:1.5b` (unos 6 GB).

   ```bash
   docker compose up -d --build
   ```

3. Para ver qué hace:

   ```bash
   docker compose logs -f sistema
   ```

   Cada nota aparece con su transcripción, lo que tardó, el diálogo con el operador y los eventos. El latido, la tablet y las alertas se ven en el visor de la planta: http://localhost:8081/?instructor

Para apagarlo, `docker compose down`. Con `-v` también borra la base.

No hay que correr al mismo tiempo el sistema de ejemplo de la planta (`planta_sim.ejemplo_sistema`): los dos le contestarían a la misma tablet.

## Tests

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

En Linux o macOS, `.venv/bin/python` en lugar de `.venv/Scripts/python`.

## Evaluación

**Corrector de la planta.** Desde el repo de la planta, con este repo al lado:

```bash
python correccion/corregir.py --repo ../marcelo-illanes --escenarios martes-tranquilo fuga-sin-enlace
```

Última corrida: **88,9 / 100, aprueba**. A1, A4a, A4b, B1, B2, F1 y F2 al 100 %. A2 y A3 al 69 %: las 10 notas con lecturas se confirmaron al primer borrador; las que no se confirman son las que no tienen ninguna lectura con número ("la bomba está parada, en reserva" y la nota de la fuga), que el operador virtual no puede confirmar porque solo compara valores.

**Parte contra la verdad de campo** ([evaluacion/parte.py](evaluacion/parte.py), resultados en [evaluacion/resultados_parte.md](evaluacion/resultados_parte.md)). Usa las corridas del corrector (verdad de campo y transcripciones reales) y compara campo por campo cada extractor:

```bash
.venv/Scripts/python evaluacion/parte.py
```

| Extractor | Equipo y variable | Valor | Unidad | Notas perfectas | Tiempo por nota |
|---|---|---|---|---|---|
| Reglas | 95 % | 95 % | 95 % | 92 % | menos de 1 ms |
| qwen3.5:2b (Ollama, 1,5 CPU) | 27 % | 18 % | 27 % | 23 % | 62 s |
| qwen2.5:1.5b (Ollama, 1,5 CPU) | 5 % | 5 % | 5 % | 15 % | 85 s |

Preguntó en las 3 notas con ambigüedad y en ninguna de las 9 claras.

**RAG** ([evaluacion/rag.py](evaluacion/rag.py)): ver [RAG de manuales](#rag-de-manuales).

## Cómo está armado

```text
docker-compose.yml   sistema, ollama y postgres, en planta-lan, con el label academia.planta: pc y el proxy de la planta
Dockerfile           imagen del sistema (Python 3.12) con Whisper adentro
ollama/Dockerfile    Ollama 0.33.2 con el modelo de lenguaje adentro
datos/materiales.yaml  repuestos, stock, lista de materiales y tareas (copia de la planta: la API no los sirve)
src/sistema/
  __main__.py        arranque
  config.py          configuración por variables de entorno
  bus.py             MQTT: suscripción a planta/... y publicación de mensajes del contrato
  despacho.py        qué se hace con cada mensaje: sensores en el hilo de red, el resto en una cola
  notas.py           cada nota de voz: bajar el audio, transcribir, buscar condiciones inseguras, alertar
  transcripcion.py   Whisper local (faster-whisper en CPU)
  seguridad.py       detector de condiciones inseguras S1 a S4, con reglas
  extractor.py       del texto de la nota a las filas del parte (FilaParte), con reglas
  llm.py             extracción con el modelo de lenguaje (LangChain + Ollama, structured output)
  numeros.py         números dichos en campo: "mil seiscientos cuarenta", "uno con cuatro", "09"
  dialogo.py         la conversación con el operador: borrador, preguntas, correcciones, confirmación
  respuestas.py      lo que contesta el operador ("No, la uno estaba en ciento quince kilos")
  eventos.py         SinNovedad / Escalado por equipo, con los límites de /api/planta
  sensores.py        desvíos sostenidos de los sensores
  historian.py       rango de 45 días y tendencia de las últimas horas (GET /api/historian)
  herramientas.py    las seis tools de solo lectura
  servidor_mcp.py    servidor MCP con las tools
  rag/               búsqueda en manuales
  equipos.py         equipos, nombres informales y límites de alarma, sacados de GET /api/planta
  planta.py          cliente HTTP de la planta, con timeouts y reintentos
  texto.py           normalización de texto hablado
  latido.py          sistema/estado cada 30 s
  enlace.py          prueba si hay enlace a internet a través del proxy
  reloj.py           hora de la planta: la de sus mensajes, no la de la máquina
  persistencia.py    Postgres: mensajes, notas, partes y uso del modelo
tests/               tests de cada pieza
evaluacion/          evaluación del parte y del RAG
docs/                mini spec del parte (spec-parte.md) y memoria y contexto (memoria.md)
INFORME.md           modelos, hardware, mediciones, tokens y costo
```

### Decisiones

- **El latido depende solo del broker.** No espera a la base ni a los modelos: en la corrección el reloj de la planta arranca con el primer latido, y un hueco de más de 60 s cuenta en contra.
- **La hora es la de la planta.** El simulador corre acelerado y con fecha propia, así que los mensajes salen con la hora de las últimas lecturas de sensores.
- **Todo mensaje que sale es un modelo de `planta_contrato`.** Si no cumple el contrato, falla al armarlo y no llega a publicarse.
- **Sesión MQTT limpia.** Al arrancar no se reciben mensajes viejos que el broker hubiera guardado para este cliente.
- **El enlace se prueba a través del proxy**, con un pedido corto a internet: cualquier respuesta HTTP quiere decir que hay enlace.
- **Las notas pasan por un carril rápido.** Una por vez y en orden de llegada: bajar el audio, transcribir y buscar condiciones inseguras. La alerta sale antes de guardar nada y antes del borrador del parte, porque el corrector da 30 s reales desde que la planta publica la nota.
- **La alerta de seguridad es en código, sin modelos.** Reglas por condición que piden algo específico ("agua a presión", no solo "agua"), descartan lo negado ("no hay olor a gas", "nadie se lastimó") y, cuando la señal es ambigua ("se cortó" puede ser la luz), piden una persona o una parte del cuerpo en la misma oración. Las notas normales de la planta ("chorrea por la empaquetadura", "está rebalsando a la pileta") están en los tests como casos que no tienen que alertar.
- **El parte lo arman las reglas, y el modelo de lenguaje es el respaldo.** Para cada palabra ancla ("descarga", "diferencial", "metros", "grados", "empaquetadura", "parada"...) las reglas toman el equipo nombrado antes y el número dicho justo antes, sin depender de la puntuación de Whisper y tolerando sus errores ("medio" por "me dio", "09" por "cero nueve", "golpe teometálico"). Medido contra la verdad de campo, aciertan el 95 % de los campos en menos de 1 ms; los modelos de lenguaje que entran en la PC de planta, con 1,25 a 1,5 CPU, aciertan entre 5 y 27 % y tardan más de un minuto por nota. Por eso el modelo (LangChain, structured output con Pydantic estricto) entra solo cuando las reglas no sacan ninguna fila de una nota que dice algo, con un llamado por nota, 400 tokens de salida y 60 s de timeout. Su salida pasa por las mismas normalizaciones que las reglas.
- **Pregunta solo cuando hace falta.** "La bomba" a secas: pregunta cuál con opciones, antes de mostrar el borrador. Un diferencial dicho "dieciocho": lo anota como 1,8 y lo muestra con opciones para confirmar. Un valor fuera de lo que marcó el equipo en los últimos 45 días se avisa en el mismo borrador, que de todos modos se confirma. Las notas claras van directo al borrador, sin preguntas.
- **Las correcciones del operador se aplican.** Valor, equipo, unidad, lectura que faltó o que sobra; después vuelve a mandar el borrador, hasta 4 veces. Las notas sin lecturas con número (por ejemplo, "la tres está parada, en reserva") se muestran sin pedir confirmación.
- **Los eventos salen del parte cerrado.** Un evento por equipo: `Escalado` si el parte dice algo anormal (cualquier observación salvo "parada, en reserva" o "parada por mantenimiento") o si una lectura pasa un límite de alarma de `/api/planta`; si no, `SinNovedad`. El escalado lleva las fotos del equipo y la tendencia de las últimas 6 horas. Ids fijos por nota y equipo (`EV-<nota>-<tag>`).
- **Los sensores escalan solo desvíos sostenidos.** Una lectura fuera de los límites de alarma tiene que seguir afuera 10 minutos de planta; un pico aislado no escala. Una bomba parada (presión de descarga cerca de la de succión) no es presión baja.
- **La conversación de cada nota queda en Postgres** (tabla `partes`: estado, intento, último mensaje, filas). Si el sistema se reinicia, retoma las que esperaban respuesta. Si el operador no contesta en 150 s reales, el parte se cierra con lo que hay.
- **Recursos.** Entre todos los servicios de planta, como mucho 4 CPU y 8 GB: sistema 2 CPU / 3 GB, ollama 1,25 CPU / 2 GB y postgres 0,5 CPU / 512 MB.

Más detalle: [mini spec del parte](docs/spec-parte.md), [memoria y constructor de contexto](docs/memoria.md) e [informe técnico](INFORME.md).

### Transcripción

Whisper small con [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2, int8, CPU), con el idioma fijo en castellano, filtro de voz (VAD) y un prompt con el vocabulario de la planta. Cambiar de modelo es cambiar `STT_MODELO` en el Dockerfile.

Medido sobre 10 notas reales de la planta (5 a 20 s de audio), con 2 CPU:

| Modelo | Tiempo por nota | Peor nota | Errores por palabra |
|---|---|---|---|
| Whisper base, beam 1 | 1,5 s | 2,3 s | 33 % |
| **Whisper small, beam 1** | **4,4 s** | **6,2 s** | **25 %** |
| Whisper small, beam 5 | 6,0 s | 9,3 s | 27 % |

El error por palabra exagera: Whisper escribe los números con cifras ("1630") y el texto de referencia con letras ("mil seiscientos treinta"). Los errores que importan son otros, como "medio" por "me dio" o "la huma" por "la bomba" (este último solo con base). En las corridas del corrector cada nota se transcribió en 2,6 a 3,7 s y la alerta de la fuga salió a los 5,3 s.

## Tools y MCP

Seis tools de solo lectura ([herramientas.py](src/sistema/herramientas.py)), cada una con entrada y salida Pydantic `extra="forbid"`, timeout, reintentos ante errores de red o 5xx y errores devueltos como un objeto (`tipo`, `mensaje`, `reintentable`), nunca como excepción:

| Tool | Fuente | Qué devuelve |
|---|---|---|
| `equipos` | `GET /api/planta` | equipos, nombres informales, unidad del manómetro y límites de alarma |
| `historial_sensores` | `GET /api/historian` | resumen de una ventana de hasta 7 días (mín., máx., promedio, tendencia, lecturas fuera de alarma) y muestras reducidas |
| `historial_ordenes` | `GET /api/historial-ordenes` | órdenes de un equipo, las más recientes primero, hasta 50 |
| `materiales` | `datos/materiales.yaml` | repuestos con costo y stock por almacén, y la lista de materiales de cada equipo |
| `catalogo_tareas` | `datos/materiales.yaml` | tareas estándar y no estándar, con horas, ejecutor y repuestos |
| `buscar_manuales` | RAG | fragmentos de los manuales con su cita (`TX350-D §3.2`) |

El servidor MCP ([servidor_mcp.py](src/sistema/servidor_mcp.py)) corre adentro del servicio `sistema`, por HTTP streamable en `http://sistema:8000/mcp` dentro de `planta-lan`. También se puede correr aparte, por HTTP o por stdio:

```bash
python -m sistema.servidor_mcp --stdio
```

## RAG de manuales

[src/sistema/rag/](src/sistema/rag/__init__.py) indexa los documentos de la planta (manuales de la bomba y del filtro, buenas prácticas, contrato e historial de órdenes; 701 fragmentos que baja de `http://sim:8100/documentos/`) en pgvector, con metadatos de equipo, fabricante, documento, sección, cita y vigencia. La búsqueda es híbrida: vectores (`jinaai/jina-embeddings-v2-base-es`, local, en CPU) más BM25, con ajustes por metadatos.

Las trampas del corpus:
- **Rev. C del manual de la bomba.** Está reemplazada: no sale por defecto, y si se piden también las no vigentes, sale abajo de la Rev. D.
- **Secciones que no aplican.** Otros modelos (TX-250, TX-500, FC-24, FC-96), otras plantas (PIA-1, PIA-2), opcionales e instalación quedan marcadas con su motivo y bajan, salvo que la consulta las nombre.
- **Códigos de falla del fabricante.** `TX-T07`, `FC-T02` y los demás se traducen a F01–F10 con la tabla del MBP-4 §6, al indexar y en la consulta.

Cada fragmento conserva su cita (`TX350-D §3.2`, `historial OT-25-0026`): lo que se concluya con él cita la sección de la que sale.

Medido con el evaluador oficial de la planta (`corpus.evaluar`, 71 preguntas de `retrieval.json`):

| Buscador | hit@5 | recall@5 | MRR | distractor@5 | Rev. C antes que D |
|---|---|---|---|---|---|
| BM25 de la planta (línea de base) | 0,85 | 0,66 | 0,57 | 0,10 | 42 de 71 preguntas |
| **Híbrido jina + BM25 + metadatos (el del sistema)** | **0,93** | **0,76** | **0,78** | **0,00** | **0** |

Detalle, otros modelos de embeddings comparados y la traducción de códigos en [evaluacion/resultados_rag.md](evaluacion/resultados_rag.md). Para reproducirlo:

```bash
.venv/Scripts/python evaluacion/rag.py
```

**Qué sale del RAG y qué no.** El RAG da el contexto de los manuales y del historial, con su cita. Los límites de alarma salen de `GET /api/planta` y el stock, los costos y las tareas, de los datos estructurados (tools `equipos`, `materiales`, `catalogo_tareas`): son datos exactos que no pueden depender de qué fragmento se recuperó, y la Rev. C del manual tiene otros límites.

**Indexado.** Al arrancar, en un hilo de prioridad mínima que le cede la CPU a Whisper, y solo si los documentos o el modelo cambiaron. La primera vez tarda unos 4 minutos (229 s medidos); los arranques siguientes, menos de un segundo. Mientras tanto, `buscar_manuales` contesta que se está preparando.

## Qué corre en la planta y qué en la nube

Todo corre en la planta. La transcripción, la alerta de seguridad, el parte y los eventos tienen que andar con el enlace caído (en `fuga-sin-enlace` se corta a los 10 minutos), y el curso definió que los modelos son locales y entran en la PC de planta (sin GPU, 4 CPU y 8 GB). Ningún dato sale de la planta: el proxy `enlace` solo se usa para saber si hay enlace.

## Contrato

`planta_contrato` se instala con pip desde el repo público de la planta, fijado al commit `ab65b95` (ver `pyproject.toml`). El link de `docs/contrato.md` apunta a `academia2026-planta`, que no existe.

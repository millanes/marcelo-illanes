# Informe técnico · marcelo-illanes

TP de la Academia Novit 2026, opción A (planta de inyección de agua), Entrega 1.

## Dónde corre cada cosa

Todo corre en la PC de la planta: sin GPU, 4 CPU y 8 GB entre todos los servicios. No se usa ningún modelo
en la nube. La transcripción, la detección de condiciones inseguras, el parte y los eventos tienen que andar
con el enlace caído (en `fuga-sin-enlace` se corta a los 10 minutos), y el curso definió que los modelos son
locales y entran en esa PC.

| Servicio | CPU | Memoria | Qué hace |
|---|---|---|---|
| `sistema` | 2 | 3 GB | MQTT, transcripción (Whisper), reglas, diálogo, eventos, RAG y servidor MCP |
| `ollama` | 1,25 | 2 GB | Modelo de lenguaje local |
| `postgres` | 0,5 | 512 MB | Postgres 16 con pgvector |
| **Total** | **3,75** | **5,5 GB** | |

## Modelos por tarea

| Tarea | Modelo | Proveedor | Versión | Parámetros | Dónde corre |
|---|---|---|---|---|---|
| Transcripción de las notas de voz | Whisper small, convertido a CTranslate2 (`Systran/faster-whisper-small`) | OpenAI (modelo), SYSTRAN (conversión y faster-whisper 1.2.1) | `small`, int8 | 244 M | Contenedor `sistema`, CPU, 2 hilos |
| Extracción del parte cuando las reglas no alcanzan | `qwen2.5:1.5b` (Qwen2.5 1.5B Instruct, cuantizado Q4_K_M) | Alibaba Cloud (Qwen), servido con Ollama 0.33.2 | `qwen2.5:1.5b` | 1,5 B | Contenedor `ollama`, CPU, 1,25 CPU |
| Embeddings de los manuales (RAG) | `jinaai/jina-embeddings-v2-base-es` (768 dimensiones) | Jina AI, servido con fastembed 0.9.1 (ONNX Runtime) | v2 base es | ~160 M | Contenedor `sistema`, CPU, 2 hilos con prioridad mínima |
| Detección de condiciones inseguras, armado del parte, eventos, desvíos de sensores | Reglas en código, sin modelo | | | | Contenedor `sistema` |

Modelos que se probaron y se descartaron:

| Modelo | Por qué no |
|---|---|
| Whisper base | 1,5 s por nota pero 33 % de error por palabra y errores que cambian el parte ("la huma" por "la bomba") |
| `qwen3.5:2b` (Ollama) | 27 % de aciertos en equipo y variable, 18 % en valor, 62 s por nota, 2,7 GB |
| `gemma4:e2b` (Ollama) | 4,6 GB: no entra en la memoria que queda junto con Whisper y Postgres |
| `paraphrase-multilingual-MiniLM-L12-v2` (embeddings) | Más liviano (0,22 GB) pero mira solo 128 tokens: híbrido hit@5 0,86 y recall@5 0,69, contra 0,93 y 0,76 |
| `embeddinggemma-300m` (embeddings) | Calidad pareja con jina (recall@5 0,77) pero pesa el doble (1,24 GB) |
| `potion-multilingual-128M` (embeddings) | Indexa en 0,2 s, pero da el peor resultado (híbrido hit@5 0,82) |

## Hardware de las pruebas

Notebook con Windows 11 y Docker Desktop (WSL2), 8 CPU y 7,8 GB asignados a Docker, sin GPU. Cada servicio
corre con los límites de la tabla de arriba, que son los de la PC de planta. Las mediciones de modelos de
lenguaje se hicieron con Ollama 0.33.2 limitado a 1,5 CPU y 4 GB.

## Mediciones

**Transcripción** (10 notas reales de la planta, 5 a 20 s de audio, 2 CPU):

| Modelo | Tiempo por nota | Peor nota | Errores por palabra |
|---|---|---|---|
| Whisper base, beam 1 | 1,5 s | 2,3 s | 33 % |
| Whisper small, beam 1 (elegido) | 4,4 s | 6,2 s | 25 % |
| Whisper small, beam 5 | 6,0 s | 9,3 s | 27 % |

En las corridas del corrector: 2,6 a 3,7 s por nota; la alerta de la fuga S1 salió a los 5,3 s de llegar la
nota (el límite es 30 s).

**Parte contra la verdad de campo** (13 notas de las corridas del corrector, transcripciones reales;
detalle en `evaluacion/resultados_parte.md`):

| Extractor | Equipo y variable | Valor | Unidad | Notas perfectas | Tiempo por nota |
|---|---|---|---|---|---|
| Reglas | 95 % | 95 % | 95 % | 92 % | menos de 1 ms |
| `qwen3.5:2b` | 27 % | 18 % | 27 % | 23 % | 62 s |
| `qwen2.5:1.5b` | 5 % | 5 % | 5 % | 15 % | 85 s |

**Corrector de la planta** (`martes-tranquilo` y `fuga-sin-enlace`): 88,9 / 100. A1, A4a, A4b, B1, B2, F1 y F2
al 100 %; A2 y A3 al 69 %, que es el techo con estos escenarios (las notas sin lecturas con número no se
pueden confirmar).

**RAG** (evaluador oficial de la planta, 71 preguntas; detalle en `evaluacion/resultados_rag.md`):

| Buscador | hit@5 | recall@5 | MRR | distractor@5 | Rev. C antes que D |
|---|---|---|---|---|---|
| BM25 de la planta (línea de base) | 0,85 | 0,66 | 0,57 | 0,10 | 42 de 71 |
| Híbrido jina + BM25 + metadatos | 0,93 | 0,76 | 0,78 | 0,00 | 0 |

Indexar los 701 fragmentos: 229 s con 2 hilos de prioridad mínima, mientras Whisper cargaba y transcribía
(Whisper cargó en 2,6 s). Consulta: unos 40 ms de embedding más 6,5 ms en pgvector.

## Tokens y costo

| Tarea | Tokens por llamado | Llamados en las corridas del corrector | Costo |
|---|---|---|---|
| Transcripción (Whisper) | no usa tokens de texto: ~3 s de CPU por nota | 1 por nota | 0 |
| Extracción con `qwen2.5:1.5b` | ~150 de entrada (instrucciones y nota) y ~120 de salida | 0: las reglas resolvieron todas las notas | 0 |
| Embeddings del RAG | no usa un modelo de lenguaje: 701 fragmentos embebidos una vez al indexar y 1 embedding por consulta | 0 consultas (el corrector no las usa) | 0 |
| Seguridad, parte, eventos, sensores | sin modelo | | 0 |

Todos los modelos son locales: no hay costo por token. El costo es el de la PC de planta, que ya existe.
Cada llamado al modelo de lenguaje queda registrado con el modelo, los tokens de entrada y salida y el tiempo
en la tabla `usos_modelo` de Postgres.

## Conclusiones

- **Lo crítico va en código.** La alerta de seguridad, los eventos y el parte de la planta los resuelven
  reglas deterministas: tardan milisegundos, dan siempre lo mismo y andan sin enlace. La alerta de la fuga
  salió en 5 s, sin falsas alarmas.
- **Whisper small es el modelo que hace la diferencia.** Base es tres veces más rápido, pero sus errores
  cambian el parte; small entra holgado en los 30 s de la alerta.
- **Un modelo de lenguaje en esta PC no mejora el parte.** Con 1,25 a 1,5 CPU, los modelos que entran en la
  memoria generan 2 a 3 tokens por segundo y aciertan mucho menos que las reglas. Queda integrado con
  LangChain y structured output como respaldo, para notas que las reglas no entienden, con presupuesto de
  tokens y timeout por nota. Si la planta tuviera más CPU o una GPU, es el componente que conviene volver a medir.
- **El RAG mejora con metadatos más que con el modelo.** Solo con BM25 y los metadatos (vigencia, modelo y
  planta a la que aplica, traducción de códigos de falla) ya se elimina la Rev. C y sube el MRR de 0,57 a
  0,76; los embeddings suman recall (0,71 a 0,76) y aciertan más en límites y operación. Límites y stock no
  salen del RAG: salen de la API y de los datos estructurados.

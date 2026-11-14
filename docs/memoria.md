# Memoria del sistema y contexto

## Qué se recuerda, dónde y por cuánto tiempo

Todo queda en el Postgres de la planta (en el compose, volumen `datos-postgres`). La memoria de trabajo de
cada conversación vive en el proceso y se respalda en Postgres en cada cambio.

| Qué | Tabla | Para qué | Cuánto tiempo |
|---|---|---|---|
| Cada nota: transcripción, condición insegura, tiempos | `notas` | Trazabilidad del parte y evaluación | 2 años (como el historial de órdenes) |
| Estado de la conversación de cada parte: estado, intento, último mensaje, filas | `partes` | Retomar después de un reinicio; auditar qué se confirmó | Abiertas: hasta que se cierran (150 s sin respuesta las cierra). Cerradas: 2 años |
| Mensajes que entran y salen por MQTT (sin los sensores) | `mensajes` | Depurar y reconstruir lo que pasó | 90 días |
| Llamados al modelo de lenguaje: tokens y tiempo | `usos_modelo` | Registro de consumo y costo | 1 año |
| Fragmentos de los manuales con su embedding | tabla del RAG | Búsqueda en manuales | Mientras el documento esté en el corpus; se reindexa si cambia |

No se guarda:

- **El audio.** Se baja, se transcribe y se descarta. Si hace falta, la planta lo sigue sirviendo en su `audio_url`.
- **Las lecturas de sensores.** El historial ya está en `/api/historian`; el sistema solo guarda la última
  de cada variable en memoria, para la hora de la planta y la detección de desvíos.
- **Lo que el operador dice fuera del parte.** Una nota no cambia reglas, límites ni el comportamiento del
  sistema: el texto de una nota es un dato, nunca una instrucción.

La depuración por antigüedad todavía no está automatizada: es una tarea programada que se suma con las
órdenes (Entrega 2). Los plazos de arriba son la política que va a aplicar.

## Constructor de contexto (diseño para la Entrega 2)

Cuando haya que analizar un escalado (causa probable, tareas, repuestos), el contexto que recibe el modelo
se arma en código, con un presupuesto fijo y en este orden:

1. **El hecho.** El parte confirmado (filas, observaciones, fotos) y el evento, con hora de planta. Siempre entra.
2. **El estado del equipo.** Tendencia de las últimas horas de los sensores relacionados (`/api/historian`),
   resumida en mín/máx/promedio y sentido. No van muestras crudas.
3. **El historial.** Las últimas órdenes del mismo equipo (`/api/historial-ordenes`), porque el historial
   cambia la causa probable cuando una falla se repite.
4. **El manual.** Los fragmentos que devuelve la búsqueda en manuales, solo vigentes (la Rev. C de la bomba
   queda afuera) y del modelo que corresponde (no TX-250, TX-500, FC-24, FC-96, PIA-1 ni PIA-2), cada uno
   con su cita (`TX350-D §7.4`). Los códigos de falla del fabricante se traducen con MBP-4 §6.
5. **Lo que se puede hacer.** Tareas estándar y repuestos con stock del equipo, por herramienta (datos
   estructurados), nunca por RAG.

Reglas del constructor:

- Límites de alarma, stock y costos salen de la API o de los datos estructurados, no del RAG.
- Cada fragmento conserva su cita: toda conclusión del modelo tiene que citar la sección de la que sale.
- Presupuesto por sección (en tokens) y recorte por relevancia; si no entra, se resume, no se corta a la mitad.
- El texto de notas, órdenes y manuales va marcado como dato: no puede cambiar las reglas de aprobación,
  que se aplican en código.

# Spec: parte de recorrida por voz

Mini spec del flujo que va de una nota de voz a un parte confirmado y sus eventos. El código sigue esta
spec y los tests de `tests/` verifican cada criterio de aceptación.

## Entradas y salidas

| | Tópico o fuente | Modelo |
|---|---|---|
| Entra | `planta/recorridas` | `NotaRecorrida` (id, ts, operador, audio_url, fotos, recorrida_id) |
| Entra | `GET {audio_url}`: WAV 16 kHz mono | |
| Entra | `planta/respuestas` | `RespuestaOperador` (mensaje_id, texto libre) |
| Entra | `GET /api/planta` | equipos, nombres informales, unidad del manómetro de cada bomba, límites de alarma |
| Entra | `GET /api/historian/{tag}/{variable}` | rango de los últimos 45 días, tendencia de las últimas 6 h |
| Sale | `sistema/tablet` | `MensajeTablet`: aviso de seguridad, pregunta, borrador, aviso de lo anotado, confirmación |
| Sale | `sistema/eventos` | `EventoSistema`: `AlertaSeguridad`, `Escalado`, `SinNovedad` |

## Flujo

1. **Transcribir.** Bajar el audio y transcribirlo en la planta (Whisper small, CPU). Sin nube.
2. **Seguridad primero.** Buscar S1 a S4 en el texto con reglas. Si hay una condición insegura, publicar en
   la tablet el aviso de aplicar el procedimiento de emergencia y avisar por radio al supervisor
   (`espera_respuesta: false`) y el evento `AlertaSeguridad` con el `nota_id`, antes de seguir.
3. **Armar el parte.** Una fila (`FilaParte`) por equipo y variable, aunque la nota los mezcle:
   - el equipo se resuelve contra los nombres informales de `/api/planta`;
   - el número se lee como se dice en campo ("mil seiscientos cuarenta", "uno con cuatro", "09");
   - la unidad es la que se dijo ("kilos" = kg/cm², "libras" = psi) o la del manómetro de la bomba
     (kg/cm² B-101, psi B-102, bar B-103);
   - las fotos se asocian a la fila de la frase donde se dijo "le saqué foto";
   - la hora del parte es el `ts` de la nota, no la de la máquina.
   Primero reglas; el modelo de lenguaje (structured output) entra cuando las reglas no sacan ninguna fila
   de una nota que dice algo (`EXTRACTOR=mixto`).
4. **Preguntar solo si hace falta.**
   - "La bomba" sin decir cuál: pregunta con opciones (una por bomba, sin filas) antes del borrador.
   - Diferencial dicho como entero ("doce", "dieciocho"): se anota dividido por diez y el borrador lleva
     opciones para confirmarlo.
   - Valor fuera del rango de los últimos 45 días: se avisa en el texto del borrador (que igual se confirma),
     sin opciones.
   - Nota clara: borrador directo, sin opciones.
5. **Borrador.** `MensajeTablet` con las filas en `pantalla.filas` y `espera_respuesta: true`.
6. **Respuesta.** Confirma → parte `Confirmado`, aviso de que se guardó. Corrige (valor, equipo, unidad,
   lectura que faltó o que sobra) → se aplica y se manda otro borrador. Una corrección que no cambia nada
   no repite el borrador.
7. **Cerrar y detectar.** Con el parte cerrado, un evento por equipo: `Escalado` si hay una observación
   anormal o una lectura fuera de los límites de alarma; si no, `SinNovedad`. Sin equipos: un `SinNovedad`
   para la nota. El escalado lleva fotos y tendencia.

Notas sin lecturas con número ("la tres está parada, en reserva", "sin novedad") y notas de condiciones
inseguras: se muestran en la tablet sin pedir confirmación y se cierran. Notas `VIVO-…`: se anota lo que se
entendió, sin preguntar.

## Estados del parte

```text
nueva ──► pregunta ──(elige)──► borrador ──(confirma)──► confirmado
   │                               │ ▲
   │                               │ └──(corrige, hasta 4 borradores)
   │                               ├──(4 borradores o no se entiende)──► sin_confirmar
   │                               └──(150 s reales sin respuesta)──► sin_respuesta
   └──(sin lecturas con número, seguridad o VIVO)──► informado
```

Cada cambio de estado queda en la tabla `partes` de Postgres (nota, texto, estado, intento, último mensaje,
filas). Si el sistema se reinicia, retoma los partes en `pregunta` o `borrador`.

## Condiciones de corte y presupuestos

| Qué | Límite |
|---|---|
| Borradores por nota | 4 |
| Espera de una respuesta del operador | 150 s reales; después se cierra con lo que hay |
| Llamados al modelo de lenguaje por nota | 1, sin reintentos ni herramientas |
| Tokens de salida del modelo por llamado | 400 (`num_predict`); contexto de 2048 |
| Timeout del modelo por llamado | 60 s; si se pasa o no valida, sigue con las reglas |
| Aviso de seguridad | antes de cualquier otra cosa de la nota; medido: 5 a 7 s |

## Criterios de aceptación

| Criterio | Dónde se verifica |
|---|---|
| Toda nota tiene un mensaje con filas en la tablet | `tests/test_dialogo.py`, criterio A1 del corrector |
| Una nota clara no genera preguntas | `test_borrador_confirmado_al_primer_intento`, A4b |
| "La bomba" a secas pregunta cuál antes del borrador | `test_la_bomba_a_secas_pregunta_cual_antes_del_borrador`, A4a |
| "Dieciocho" de diferencial se anota 1,8 y se pregunta | `test_un_diferencial_dicho_dieciocho_se_anota_uno_ocho_y_se_pregunta` |
| Las correcciones del operador se aplican | `test_aplica_las_correcciones_y_manda_otro_borrador`, A3 |
| Condición insegura: aviso y evento en menos de 30 s | `tests/test_notas.py`, `tests/test_seguridad.py`, B2 |
| Sin falsas alarmas en notas normales | `test_las_notas_normales_no_son_alertas`, B2 |
| Un evento por equipo, Escalado solo con anomalía | `tests/test_eventos.py`, B1 |
| Todo lo que sale cumple el contrato | los tests validan cada mensaje con `planta_contrato`, F1 |

## Fuera de alcance (Entrega 1)

Análisis de causa, órdenes de trabajo, aprobación, cola de sincronización con la base y cierre de órdenes.

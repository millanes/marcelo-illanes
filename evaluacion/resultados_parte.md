# Evaluación del parte contra la verdad de campo

13 notas de las corridas del corrector (martes-tranquilo, fuga-sin-enlace), con la transcripción real de Whisper small. Se comparan las lecturas con número que dijo el operador (sin el goteo, que no dice en voz alta).

| Extractor | Equipo y variable | Valor | Unidad | Lecturas de más | Notas perfectas | Marca bien la ambigüedad | Tiempo medio por nota |
|---|---|---|---|---|---|---|---|
| reglas | 95 % | 95 % | 95 % | 1 | 92 % | 100 % | 0 ms |

Los modelos de lenguaje se midieron con `--llm` sobre la corrida anterior del corrector (otras 13 notas del
mismo tipo; las reglas dieron ahí los mismos 95 %):

| Extractor | Equipo y variable | Valor | Unidad | Lecturas de más | Notas perfectas | Marca bien la ambigüedad | Tiempo medio por nota |
|---|---|---|---|---|---|---|---|
| modelo (qwen3.5:2b) | 27 % | 18 % | 27 % | 11 | 23 % | 54 % | 61933 ms |
| modelo (qwen2.5:1.5b) | 5 % | 5 % | 5 % | 10 | 15 % | 38 % | 85444 ms |

Corrieron en Ollama 0.33.2 con 1,5 CPU y 4 GB, como en la PC de planta, con structured output (esquema
`ParteModelo`). Generaron a 2 a 3,5 tokens por segundo, y la máquina estaba además ocupada con otras pruebas,
así que el tiempo exagera un poco; los aciertos no dependen de eso. Errores típicos del modelo:
"equipo: Pistola", "variable: nivel" para una presión de descarga, valores que no se dijeron.

## ¿Preguntó solo cuando hacía falta?

- Notas con ambigüedad en las que preguntó: 3 de 3.
- Notas claras en las que preguntó de más: 0 de 9.

## Notas

| Escenario | Nota | Lo que dijo | Lo que transcribió Whisper |
|---|---|---|---|
| martes-tranquilo | NOTA-00002 | El de la izquierda me dio uno con cuatro de diferencial. La dos está en mil seiscientos veinte psi de descarga. | El de la izquierda me dio uno con cuatro de diferencial. La dos está en 1620 psi de descarga. |
| martes-tranquilo | NOTA-00003 | A ver, la de la punta está en ciento catorce de descarga. El secundario me dio uno de diferencial. | A ver, la de la punta está en 114 de descarga. El secundario me dio uno de diferencial. |
| martes-tranquilo | NOTA-00005 | El pulmón está en cinco metros. | El pulmón está en 5 metros. |
| martes-tranquilo | NOTA-00008 | La bomba está parada, en reserva. | La bomba está parada en reserva. |
| martes-tranquilo | NOTA-00007 | Listo, la del medio está en mil seiscientos diez libras de descarga. El filtro A me dio uno coma tres de diferencial. | Listo, la del medio está en 1610 libras de descarga. El filtro A me dio 1,3 de diferencial. |
| martes-tranquilo | NOTA-00006 | La de la punta está en ciento doce de descarga. El de la derecha me dio cero nueve de diferencial. | La de la punta está en 112 de descarga. El de la derecha me dio 09 de diferencial. |
| martes-tranquilo | NOTA-00010 | El filtro B me dio cero nueve de diferencial. El pulmón está en cinco metros. | El filtro B medio 0,9 de diferencial, el pulmón está en 5 metros. |
| martes-tranquilo | NOTA-00011 | La uno está en ciento doce kilogramos de descarga, el rodamiento me dio cincuenta y nueve grados con la pistola. También la bomba está en mil seiscientos psi de descarga. El filtro A me dio uno coma tres de diferencial. | La uno está en 112 kilogramos de descarga, el rodamiento medió 59 grados con la pistola, Ambient la bomba está en 1600 pese y de descarga, el fito a medio 1,3 de diferencial. |
| martes-tranquilo | NOTA-00013 | Bueno, la dos está en mil quinientos noventa libras de descarga. El filtro A me dio uno tres de diferencial. La de la punta está en ciento doce kilos de descarga. | Bueno, la dos está en 1590 libras de descarga. El filtro A medio uno tres de diferencial. La de la punta está en 112 kilos de descarga. |
| fuga-sin-enlace | NOTA-00001 | ¡Atención! Se rompió algo en la descarga de la uno, sale agua a presión para todos lados, me alejé. Saqué foto desde lejos. | Atención, se rompió algo en la descarga de la uno, sale agua a presión para todos lados, me alejé. Saqué foto desde lejos. |
| fuga-sin-enlace | NOTA-00006 | Listo, la bomba está parada, en reserva. | Punto listo, la bomba está parada en reserva. |
| fuga-sin-enlace | NOTA-00004 | La dos está en mil seiscientos diez libras de descarga. El pulmón está en cinco metros. | La dos está en 1610 libras de descarga. El pulmón está en 5 metros. |
| fuga-sin-enlace | NOTA-00005 | La uno está en ciento catorce kilos de descarga, el rodamiento me dio sesenta y cuatro grados con la pistola. | La uno está en 114 kilos de descarga. El rodamiento medió 64 grados con la pistola. |

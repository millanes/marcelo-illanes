# RAG de manuales: resultados

Generado por `evaluacion/rag.py` el 13/11/2026 con el evaluador oficial de la planta (`corpus.evaluar.evaluar(buscar, suite)`): 71 preguntas de `evaluacion/retrieval.json`, 701 fragmentos, k = 1, 3, 5 y 10. Salvo que se diga otra cosa, el buscador corre como lo usa el sistema: `solo_vigentes=True` (la Rev. C no se devuelve) y sin `equipo`.

## Contra la línea de base

| Configuración | hit@1 | hit@3 | hit@5 | hit@10 | recall@1 | recall@3 | recall@5 | recall@10 | MRR | distractor@5 | Rev. C antes que D (top 5 / 10) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BM25 de la planta (línea de base) | 0,35 | 0,78 | 0,84 | 0,93 | 0,25 | 0,60 | 0,66 | 0,77 | 0,57 | 0,10 | 42 / 50 |
| BM25 propio + metadatos (sin embeddings) | 0,68 | 0,86 | 0,86 | 0,89 | 0,45 | 0,66 | 0,71 | 0,78 | 0,76 | 0,00 | 0 / 0 |
| **jina-embeddings-v2-base-es: híbrido + metadatos (elegido)** | 0,65 | 0,92 | 0,93 | 0,96 | 0,42 | 0,72 | 0,76 | 0,84 | 0,78 | 0,00 | 0 / 0 |

- **distractor@5** es la métrica oficial: una sección que la suite marca como distractor de esa pregunta (la Rev. C, otro modelo, la lista de precios que no corresponde) aparece antes que cualquier relevante. Es la "revisión vieja antes que la vigente en una de cada diez preguntas" del README del corpus.
- **Rev. C antes que D**: preguntas en las que una sección de la Rev. C aparece antes que la misma sección (misma clave) de la Rev. D, o sin ella, sea o no distractor de esa pregunta. En la línea de base son 42 de 71: los textos de la C y la D son casi iguales, empatan en BM25 y el empate se rompe a favor de la C, que está después en `fragmentos.jsonl`.
- **BM25 propio + metadatos** es el mismo buscador sin embeddings: lo que queda si el modelo no carga.

### La Rev. C, si se pide

Con `solo_vigentes=False` la Rev. C entra al ranking. Sin los ajustes por metadatos sale antes que su par de la Rev. D en 10 preguntas; con el ajuste `reemplazada` (la mitad del puntaje), en 0. Si la consulta nombra la revisión ("Rev. C"), no se castiga.

| Configuración | hit@1 | hit@3 | hit@5 | hit@10 | recall@1 | recall@3 | recall@5 | recall@10 | MRR | distractor@5 | Rev. C antes que D (top 5 / 10) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| jina-embeddings-v2-base-es, `solo_vigentes=False`, sin ajustes por metadatos | 0,56 | 0,84 | 0,89 | 0,94 | 0,37 | 0,64 | 0,70 | 0,80 | 0,71 | 0,01 | 10 / 18 |
| jina-embeddings-v2-base-es, `solo_vigentes=False`, con los ajustes | 0,63 | 0,92 | 0,93 | 0,96 | 0,42 | 0,72 | 0,76 | 0,82 | 0,77 | 0,00 | 0 / 0 |

## Modelos de embeddings

Medidos en esta PC (Windows, 8 núcleos lógicos) con 2 hilos, el límite de CPU del contenedor del sistema, cada modelo en su proceso. Memoria: lo que suma cargar el modelo y el pico del proceso al indexar (con Python y ONNX Runtime). Calidad: solo vectores (con los mismos ajustes por metadatos) y el híbrido con BM25. Tokens: hasta dónde mira cada texto.

| Modelo | Dim. | Archivos | Tokens | Solo vectores hit@5 / recall@5 | Híbrido hit@5 / recall@5 / MRR | Carga | Indexar 701 | Consulta | Memoria modelo / pico |
|---|---|---|---|---|---|---|---|---|---|
| `jinaai/jina-embeddings-v2-base-es` | 768 | 0,64 GB | 8192 | 0,83 / 0,65 | 0,93 / 0,76 / 0,78 | 2,0 s | 269 s | 40 ms | 685 / 1040 MB |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 384 | 0,22 GB | 128 | 0,58 / 0,41 | 0,86 / 0,69 / 0,66 | 2,0 s | 41 s | 11 ms | 567 / 683 MB |
| `google/embeddinggemma-300m` | 768 | 1,24 GB | 2048 | 0,80 / 0,65 | 0,90 / 0,77 / 0,78 | 3,7 s | 297 s | 54 ms | 540 / 895 MB |
| `minishlab/potion-multilingual-128M` | 256 | 0,51 GB | 512 | 0,66 / 0,49 | 0,82 / 0,62 / 0,61 | 3,7 s | 0,2 s | 0,2 ms | 1022 / 1229 MB |

Se embebe la sección sin el nombre del documento (`Fragmento.para_embeber`): en una prueba aparte con jina, el nombre del manual repetido en sus 112 secciones bajaba solo vectores de hit@5 0,83 a 0,76, porque acerca entre sí todas las secciones de un mismo manual. BM25 sí lo usa (`Fragmento.para_bm25`), y ahí suma.

## Por tipo de pregunta

| Tipo | n | recall@5 BM25 | recall@5 jina-embeddings-v2-base-es | hit@5 BM25 | hit@5 jina-embeddings-v2-base-es |
|---|---|---|---|---|---|
| certificacion | 5 | 0,90 | 0,90 | 1,00 | 1,00 |
| contrato | 7 | 0,76 | 0,81 | 0,86 | 0,86 |
| diagnostico | 14 | 0,77 | 0,82 | 1,00 | 1,00 |
| historial | 11 | 0,59 | 0,62 | 0,82 | 0,91 |
| limites | 11 | 0,42 | 0,74 | 0,64 | 0,91 |
| operacion | 11 | 0,61 | 0,73 | 0,64 | 0,82 |
| procedimiento | 8 | 0,65 | 0,69 | 1,00 | 1,00 |
| seguridad | 4 | 0,88 | 1,00 | 1,00 | 1,00 |

## Códigos de falla

21 preguntas armadas con la tabla del MBP-4 §6.1 (no son de la suite oficial): "Apareció la falla TX-T07, ¿qué hay que hacer?" tiene que traer la falla del catálogo de la planta (MBP-4 §6.11, F09) y "¿Qué dice el manual del fabricante sobre la falla F09?", la sección del fabricante (TX350-D §8.9).

| | hit@1 | hit@3 | recall@3 | MRR |
|---|---|---|---|---|
| Con la traducción (al indexar y en la consulta) | 0,52 | 1,00 | 1,00 | 0,76 |
| Sin la traducción | 0,00 | 0,05 | 0,05 | 0,09 |

Con la traducción, la primera suele ser la sección del MBP-4 y la segunda la del fabricante (por eso hit@1 queda en la mitad): las dos sirven.

## Backend pgvector

Los mismos vectores, guardados en Postgres 16 con pgvector (701 filas en 0,2 s) y buscados con SQL (`ORDER BY embedding <=> consulta`): hit@5 0,93, recall@5 0,76, MRR 0,78, y el mismo top 10 que el backend en memoria en 71 de 71 preguntas. Cada búsqueda tarda 6,6 ms sin contar el embedding de la consulta (40 ms con jina-embeddings-v2-base-es y 2 hilos).

## Conclusiones

- **Contra BM25**: hit@5 0,84 → 0,93, recall@5 0,66 → 0,76, MRR 0,57 → 0,78 y distractor@5 0,10 → 0,00. La mejora grande está en las preguntas de `limites` (recall@5 0,42 → 0,74); la más chica, en el historial (0,59 → 0,62): las órdenes son texto libre con abreviaturas y a veces lo que las une con la pregunta es la causa, no las palabras (his-03: las válvulas gastadas con arena las explica un cartucho roto del F-201B).
- **Híbrido**: ni BM25 solo (hit@5 0,86, recall@5 0,71) ni los vectores solos (hit@5 0,83, recall@5 0,65) llegan a la combinación. BM25 resuelve lo que tiene códigos, números o nombres propios (TX-VAL-KIT, 5V-1800, Servipat) y los vectores lo dicho con otras palabras ("¿a qué hora son las recorridas?" → MBP-4 §4.2, "Horario de recorridas"). Un modelo flojo resta: con MiniLM el híbrido queda abajo del mismo buscador sin vectores en MRR (tabla de modelos).
- **Rev. C**: la línea de base pone una sección de la Rev. C antes que la misma de la Rev. D en 42 de 71 preguntas (top 5) y un distractor antes que lo relevante en 7; el buscador, en 0 y 0. La Rev. C está reemplazada (MBP-4 §1.2: sus límites de vibración, temperatura y presión, el aceite ISO VG 150 y los códigos 350-xxx ya no valen; en TX350-C §3.2 la vibración dispara a 7,1 mm/s y en TX350-D §3.2 a 11,2), así que por defecto no se devuelve y, si se pide, va abajo de la D.
- **Otros modelos y plantas**: TX350-D §3.3 (TX-250 y TX-500), MBP-4 §5.3 y §17 (PIA-1 y PIA-2), los opcionales (TX350-D §12, FC48-3 §16) y la instalación o preservación (TX350-D §4 y §11) quedan marcados como que no aplican, con el motivo, y bajan salvo que la consulta nombre ese modelo o esa planta. Las tablas de la familia (TX350-D §2.1, FC48-3 §2.1) bajan un poco: la velocidad máxima de la TX-350 sale de TX350-D §3.1 y no de la tabla que la compara con la TX-250 y la TX-500.
- **Códigos de falla**: la correspondencia sale del MBP-4 §6.1 (TX-T07 = F09, FC-T02 = F08, TX-T08 y FC-T04 = F10). Con la traducción, hit@3 1,00 en las 21 preguntas de códigos; sin ella, 0,05: el código solo no dice de qué falla se trata ("TX-T07" trae la tabla de fallas, TX350-D §8.2, y no la sección de la cavitación) y "F09" no encuentra la sección de Trimax, que no lo nombra.
- **Modelo**: `jinaai/jina-embeddings-v2-base-es`. Elegido con datos: el mejor recall@5 híbrido entre los modelos que entran en la PC de la planta (archivos de hasta 1 GB y consulta de hasta 100 ms con 2 hilos) y, si otro queda a menos de una pregunta, el más liviano. Da hit@5 0,93, recall@5 0,76 y MRR 0,78; consulta en 40 ms, indexa los 701 fragmentos en 269 s (una sola vez: queda en Postgres) con un pico de 1040 MB, y es un modelo entrenado para castellano. En cambio, `paraphrase-multilingual-MiniLM-L12-v2` da recall@5 0,69 y MRR 0,66 en el híbrido, con 0,22 GB de archivos (solo mira los primeros 128 tokens de cada sección); `embeddinggemma-300m` da recall@5 0,77 y MRR 0,78 en el híbrido, con 1,24 GB de archivos (lo mismo que el elegido, a menos de una pregunta, pero pesa más de 1 GB); `potion-multilingual-128M` da recall@5 0,62 y MRR 0,61 en el híbrido, con 0,51 GB de archivos.
- **Límites y stock no salen del RAG**: el buscador encuentra las secciones (límites: recall@5 0,74), pero el dato que vale es el de GET /api/planta: el MBP-4 §5 dice que los límites de alarma configurados son los únicos válidos para PIA-3, y el MBP-4 §14.2 que el stock de su tabla es de referencia y el real es el del sistema de almacén.
- **Ajustes** (`sistema.rag.busqueda.Ajustes`): elegidos con esta misma suite, sin un conjunto aparte para validar. En la zona probada (peso de los vectores 0,4 a 0,6; equipo y pasado 1 a 1,6) hit@5 y recall@5 se mueven una o dos preguntas.

| Ajuste | Valor |
|---|---|
| `peso_vectores` | 0,5 |
| `candidatos` | 60 |
| `no_aplica` | 0,5 |
| `nombra_otros` | 0,85 |
| `reemplazada` | 0,5 |
| `equipo_nombrado` | 1,3 |
| `equipo_documento` | 1,1 |
| `fuera_de_periodo` | 0,6 |
| `pasado` | 1,3 |

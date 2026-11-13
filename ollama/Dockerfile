# Modelo de lenguaje local para la PC de planta: Ollama con el modelo adentro de la imagen.
# En la planta no hay internet garantizado (y en la corrección el enlace se corta), así que el modelo se baja
# una sola vez, al construir. Es la misma versión de Ollama que usan los ejemplos del curso.
FROM ollama/ollama:0.33.2

ARG MODELO=qwen2.5:1.5b
RUN ollama serve >/dev/null 2>&1 & pid=$!; \
    for i in $(seq 1 30); do ollama list >/dev/null 2>&1 && break; sleep 1; done; \
    ollama pull "$MODELO" && kill $pid

# Un modelo cargado y una consulta por vez: en CPU, dos a la vez tardan más que una atrás de la otra.
ENV OLLAMA_KEEP_ALIVE=-1 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1

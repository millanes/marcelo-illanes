FROM python:3.12-slim

# git: el contrato de la planta se instala con pip desde su repo público.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Primero las dependencias, con un paquete vacío: esta capa se reutiliza mientras no cambie pyproject.toml.
COPY pyproject.toml ./
RUN mkdir -p src/sistema && touch src/sistema/__init__.py \
 && pip install . \
 && pip uninstall -y sistema-planta \
 && rm -rf src build

# Modelo de transcripción adentro de la imagen: en la planta no hay internet garantizado. Se baja una vez,
# al construir (Whisper small convertido a CTranslate2, de Systran en Hugging Face).
ARG STT_MODELO=small
RUN python -c "from faster_whisper.utils import download_model; download_model('${STT_MODELO}', output_dir='/modelos/whisper')"

# Modelo de embeddings del RAG de manuales, también adentro de la imagen (fastembed, ONNX en CPU, unos 0,6 GB).
ARG RAG_MODELO=jinaai/jina-embeddings-v2-base-es
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${RAG_MODELO}', cache_dir='/modelos/embeddings')"
ENV STT_MODELO=/modelos/whisper RAG_MODELO=${RAG_MODELO} RAG_CACHE=/modelos/embeddings HF_HUB_OFFLINE=1

# Datos estructurados que la API de la planta no sirve: repuestos, stock, lista de materiales y tareas.
COPY datos ./datos
ENV MATERIALES_YAML=/app/datos/materiales.yaml

# Después el código, sin volver a resolver dependencias.
COPY src ./src
RUN pip install --no-deps .

CMD ["python", "-m", "sistema"]

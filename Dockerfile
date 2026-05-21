# ductile-docling — bespoke FastAPI worker.
#
# Single-stage build on python:3.12-slim-bookworm (glibc, so ML wheels work).
# We deliberately bake the docling layout/table model cache into the image
# during build so the first request after container start isn't a 90s cold
# parse — and so the model files land as root (writable) during build but
# are read-only at runtime under the non-root container user, which both
# saves a model download AND sidesteps the rapidocr writable-package-dir
# clash we hit on the earlier attempt.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models \
    TRANSFORMERS_CACHE=/models

# Runtime deps for docling (opencv → libgl1/libglib2.0-0) + uvicorn HTTP probes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# We use the PyTorch CPU index for torch + torchvision (~200MB total) instead
# of the PyPI default which bundles ~3GB of CUDA wheels we don't use.
# PyPI is kept as the fallback index for everything else (docling, fastapi,
# uvicorn, etc.). Flip to /whl/cu121 + add `--gpus all` in compose to use
# the unraid RTX 2070 if we ever need GPU.
COPY pyproject.toml ./
COPY worker/ ./worker/
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    .

# Ensure the model cache dir exists and is writable by the runtime user.
# docling lazy-loads models on first convert() rather than at constructor
# time, so we don't pre-populate /models at build (an actual convert would
# need a sample PDF baked in). The first request after a fresh data volume
# pays a ~30-60s model-download cost; subsequent requests are fast because
# the operator mounts /models on a persistent volume in compose.
RUN mkdir -p /models \
 && useradd --create-home --uid 1000 worker \
 && mkdir -p /workspace \
 && chown -R worker:worker /workspace /models /app
USER worker

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# Single uvicorn worker — docling holds heavy model state per-process and
# isn't safe to parallelise within one process. Scale horizontally with
# multiple containers if needed.
CMD ["uvicorn", "worker.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

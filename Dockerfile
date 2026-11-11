# syntax=docker/dockerfile:1
# LOG_ON container (post-Phase-4 step 3). Multi-stage: dependencies are built into a venv in the first stage and
# copied into a slim runtime that runs as a non-root user. No model weights, secrets or run data are baked in.
#   docker build -t log-on --build-arg GIT_SHA="$(git rev-parse HEAD)" .
#   docker build -t log-on:ml --build-arg WITH_ML=1 .      # + Presidio / spaCy / ONNX injection classifier runtime
ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS builder
ARG WITH_ML=0
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt requirements-prod.txt requirements-ml.txt ./
RUN pip install -r requirements.txt -r requirements-prod.txt \
 && if [ "$WITH_ML" = "1" ]; then pip install -r requirements-ml.txt; fi

FROM python:${PYTHON_VERSION}-slim AS runtime
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="log-on" \
      org.opencontainers.image.source="https://github.com/Logonotobscurity/log-on" \
      org.opencontainers.image.revision="${GIT_SHA}"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app \
    LOG_ON_GIT_SHA=${GIT_SHA} \
    LOG_ON_MODEL_CACHE=/home/app/.cache/log_on_models \
    LOG_ON_UI_HOST=0.0.0.0 \
    LOG_ON_UI_PORT=8765
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app log_on ./log_on
COPY --chown=app:app evals ./evals
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app samples ./samples
COPY --chown=app:app scripts ./scripts
RUN mkdir -p /app/runs /home/app/.cache && chown -R app:app /app/runs /home/app/.cache
USER 10001
VOLUME ["/app/runs", "/home/app/.cache"]
EXPOSE 8765
# Liveness of the approval UI (the default command). One-shot CLI runs disable it in docker-compose.yml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('LOG_ON_UI_PORT', '8765') + '/healthz', timeout=4)"]
CMD ["python", "-m", "log_on.approval_ui"]

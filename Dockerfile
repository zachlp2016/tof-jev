# syntax=docker/dockerfile:1
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /build
RUN python -m venv /opt/venv
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation .

FROM dependencies AS test
COPY requirements-dev.lock ./
RUN python -m pip install --no-cache-dir -r requirements-dev.lock
COPY tests ./tests
RUN python -m pytest && python -m ruff check src tests

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

LABEL org.opencontainers.image.title="TOF Prediction Adapter" \
      org.opencontainers.image.description="REST and MCP access to TOF prediction backends" \
      org.opencontainers.image.source="https://github.com/zachlp2016/tof-jev"

RUN groupadd --gid 10001 tof && useradd --uid 10001 --gid tof --no-create-home --shell /usr/sbin/nologin tof
COPY --from=dependencies /opt/venv /opt/venv
WORKDIR /app
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3).read()"]
CMD ["uvicorn", "tof_prediction_adapter.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log", "--limit-concurrency", "128"]

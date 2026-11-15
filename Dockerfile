# CloudTransfer: a streaming relay between Google Drive and OneDrive.
#
# Runs the same code locally and in a container. Nothing is written to disk
# during a transfer, so the image needs no scratch space - only a small volume
# for the SQLite job database and the generated secret key.

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY cloudtransfer ./cloudtransfer

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir .


FROM python:3.13-slim AS runtime

LABEL org.opencontainers.image.title="CloudTransfer" \
      org.opencontainers.image.description="Disk-free streaming transfer from Google Drive to OneDrive" \
      org.opencontainers.image.source="https://github.com/unilker/CloudTransfer"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    CT_DATA_DIR=/data \
    CT_HOST=0.0.0.0 \
    CT_PORT=8080

# curl is only here for the container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 relay \
 && mkdir -p /data \
 && chown -R relay:relay /data

COPY --from=builder /opt/venv /opt/venv

USER relay
WORKDIR /home/relay
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["cloudtransfer"]
CMD ["serve"]

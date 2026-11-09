# AETHER Community Edition (DEMO executor only). Stdlib Python; no pip, no network at runtime.
# Build context is the repository root.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AETHER_MODE=custom AETHER_PROVIDER=none AETHER_RUNTIME=docker \
    AETHER_DATA_DIR=/data AETHER_PORT=8080 AETHER_BIND=0.0.0.0

RUN groupadd --gid 10001 studio \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin studio \
 && install -d -o 10001 -g 10001 -m 0700 /data

WORKDIR /app
COPY aether/__init__.py aether/security.py ./aether/
COPY aether/studio ./aether/studio
RUN chmod -R a+rX,go-w /app

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
  CMD ["python", "-c", "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8080,timeout=2); c.request('GET','/healthz',headers={'Host':'127.0.0.1:8080'}); sys.exit(0 if c.getresponse().status==200 else 1)"]
ENTRYPOINT ["python", "-m", "aether.studio.app"]

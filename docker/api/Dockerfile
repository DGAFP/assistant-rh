FROM python:3.12-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV API_HOST=0.0.0.0
ENV API_PORT=8000

WORKDIR /app

RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates \
    && addgroup -g 10001 api \
    && adduser -D -u 10001 -G api api

COPY apps/api ./apps/api

RUN pip install --no-cache-dir --require-hashes -r apps/api/requirements.lock \
    && pip install --no-cache-dir --no-deps ./apps/api

USER api

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)"

CMD ["assistant-rh-api"]

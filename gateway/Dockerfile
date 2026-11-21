# syntax=docker/dockerfile:1
FROM python:3.13.18-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY llm_gateway ./llm_gateway
RUN pip install . && groupadd --system --gid 10001 gateway \
 && useradd --system --uid 10001 --gid 10001 --no-create-home gateway

USER 10001:10001
EXPOSE 8000 9400
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status != 200)"
ENTRYPOINT ["llm-gateway"]
CMD ["--config", "/etc/llm-gateway/config.yml"]

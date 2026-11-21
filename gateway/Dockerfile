# syntax=docker/dockerfile:1
# Build stage: install the gateway and its dependencies into a virtualenv.
FROM python:3.13.18-slim-trixie AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml ./
COPY llm_gateway ./llm_gateway
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install . \
 && /opt/venv/bin/pip uninstall -y pip setuptools

# Runtime stage: the virtualenv on a patched base, without pip or setuptools (never used at runtime,
# and the source of most scanner findings).
FROM python:3.13.18-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip setuptools \
 && groupadd --system --gid 10001 gateway \
 && useradd --system --uid 10001 --gid 10001 --no-create-home gateway
COPY --from=build /opt/venv /opt/venv

USER 10001:10001
EXPOSE 8000 9400
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status != 200)"
ENTRYPOINT ["llm-gateway"]
CMD ["--config", "/etc/llm-gateway/config.yml"]

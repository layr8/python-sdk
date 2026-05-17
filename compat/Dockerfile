FROM python:3.12-alpine
WORKDIR /app

ARG SDK_VERSION
RUN pip install --no-cache-dir layr8==${SDK_VERSION}

COPY scenarios/ ./scenarios/
COPY bin/ ./bin/

LABEL org.opencontainers.image.source=https://github.com/layr8/python-sdk

ENTRYPOINT ["python", "-m", "bin.compat"]
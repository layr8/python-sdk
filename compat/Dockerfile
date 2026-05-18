FROM python:3.12-alpine
WORKDIR /app

COPY sdk/ /tmp/sdk/
RUN pip install --no-cache-dir /tmp/sdk/ && rm -rf /tmp/sdk/

COPY compat/scenarios/ ./scenarios/
COPY compat/bin/ ./bin/

LABEL org.opencontainers.image.source=https://github.com/layr8/python-sdk

ENTRYPOINT ["python", "-m", "bin.compat"]
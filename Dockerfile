FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config

# Socket Mode opens an outbound websocket, so no port is exposed and no
# inbound firewall rule is needed.
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app /app/data
USER app
VOLUME ["/app/data"]

CMD ["lej-cc"]

# The outside world: simulated customers and suppliers answering Apex by real email.
# Code is mounted read-only from ./legacy (see docker-compose.legacy.yml); nothing is installed (stdlib only).
FROM python:3.12-slim
RUN useradd -m responder
USER responder
WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8

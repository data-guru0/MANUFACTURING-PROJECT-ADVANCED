# Headless LibreOffice Calc, used only to RECALCULATE spreadsheets (no GUI, runs on demand).
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-calc-nogui \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m calc
USER calc
WORKDIR /work
ENTRYPOINT ["soffice", "--headless", "--norestore", "--nolockcheck"]

# The agent's own image: self-check, knowledge sync, MCP tool servers (and later the workers).
# Dependencies install OFFLINE from wheels downloaded on the host (`make wheels`): PyPI is very slow
# from inside Docker on the lab PC, and an offline install is also reproducible from requirements.lock.
FROM python:3.12-slim
# S9: `apt-get upgrade` takes the base image's security fixes (trivy found zlib CVE-2026-85091 without it).
# Docker reuses this layer from its cache, so new Debian fixes arrive only when it is rebuilt: bump APT_REFRESH when
# `make supply-chain` (trivy) reports OS packages with a fix available (2026-11-23: krb5 1.21.3-5+deb13u1).
ARG APT_REFRESH=2026-11-23
# libmagic: the security gate's real-file-type check (S3). S4: Tesseract + Ghostscript (OCRmyPDF), libGL (OpenCascade). S8: Pango + DejaVu fonts (WeasyPrint quote PDFs)
RUN apt-get update && apt-get upgrade -y --no-install-recommends && apt-get install -y --no-install-recommends libmagic1 tesseract-ocr tesseract-ocr-eng ghostscript libgl1 libxrender1 libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 agent
WORKDIR /app
COPY requirements.lock ./
COPY .wheels /wheels
# (the spaCy model is pinned by URL in the lock; offline it is found by name among the wheels)
RUN sed -i 's| @ https://.*||' requirements.lock  && pip install --no-cache-dir --no-index --find-links /wheels -r requirements.lock && rm -rf /wheels /root/.cache
COPY src ./src
USER agent
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src HF_HUB_OFFLINE=1

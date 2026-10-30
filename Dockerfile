# Reflex: one container serves the pages and the API.
# Hugging Face Spaces (Docker SDK) expects the app on port 7860 and runs it as user 1000.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRUST_PROXY=1

RUN useradd -m -u 1000 user
WORKDIR /app

# dependencies first, so code-only changes rebuild quickly
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/src backend/src
RUN pip install ./backend

# the built pages (build.py output) and static assets: an explicit list, nothing else
COPY *.html jszip.min.js preview-card.webp web/
COPY videos web/videos

# cache + feedback databases live here; it is empty at start and resets when the Space restarts
RUN mkdir -p /app/data && chown -R user:user /app/data
USER user

EXPOSE 7860
CMD ["reflex", "serve", "--web", "/app/web", "--cache", "/app/data/cache.sqlite", "--host", "0.0.0.0", "--port", "7860"]

# Dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (keep minimal; add build-essential only if you need compiling wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app
COPY app /app/app

# Default: run API (override in docker-compose for scheduler)
ENV APP_MODE=api

# Simple entrypoint that picks a mode
CMD ["sh", "-c", "\
  if [ \"$APP_MODE\" = \"scheduler\" ]; then \
    python -m app.scheduler; \
  elif [ \"$APP_MODE\" = \"ui\" ]; then \
    python -m app.ui; \
  else \
    uvicorn app.main:app --host 0.0.0.0 --port 8000; \
  fi \
"]

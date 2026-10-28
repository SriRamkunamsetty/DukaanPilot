# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes do not invalidate this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Run as an unprivileged user; /tmp is the only writable place on Cloud Run.
RUN useradd --create-home --uid 10001 app
COPY --chown=app:app main.py ./
COPY --chown=app:app dukaanpilot ./dukaanpilot
USER app

ENV PORT=8080 \
    DUKAANPILOT_ENV=prod

EXPOSE 8080
# Shell form on purpose: Cloud Run injects $PORT.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'

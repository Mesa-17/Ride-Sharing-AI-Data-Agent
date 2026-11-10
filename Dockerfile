# Web API for the live demo (FastAPI + the SQL agent)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py ./
COPY agents ./agents
COPY api ./api
COPY models ./models
COPY utils ./utils

# Run as a non-root user
RUN useradd --create-home app && chown -R app /app
USER app

# Railway, Render and Fly set $PORT; 8000 locally
CMD ["sh", "-c", "uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]

# Single image used by both the dashboard service and the portfolio runner.
# Slim Python 3.11 — no TensorFlow (LSTM training is out-of-band).

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /app

# System deps for scientific Python wheels (most have manylinux wheels but
# scipy/xgboost occasionally need libgomp at runtime).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt

COPY . /app

# Non-root for safety
RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot

EXPOSE 8765

# Default command runs the dashboard. The portfolio service in docker-compose
# overrides this with `python -m live.portfolio_runner`.
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8765"]

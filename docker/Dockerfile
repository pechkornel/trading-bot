# One Dockerfile, three named stages:
#   deps    system packages and requirements.txt (shared base)
#   keeper  deps plus the headless gateway login (Playwright, Chromium headless
#           shell only). Used by the cpgw_keeper service alone.
#   app     deps plus the code. Listed last so it is the default target, used by
#           every other bot service.
# Slim Python 3.11, no TensorFlow (LSTM training is out-of-band).

FROM python:3.11-slim AS deps

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


FROM deps AS keeper

# The headless shell build of Chromium needs no X server or virtual display. The
# browser is installed as root into a fixed path, so the bot user can read it.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

COPY requirements-login.txt /app/requirements-login.txt
RUN pip install -r requirements-login.txt \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

# Non-root. uid 1000 must match the host owner of secrets/ibkr_login, because
# core/secret_store.py requires the reading uid to own the secret file.
RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot


FROM deps AS app

COPY . /app

# Non-root for safety
RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot

EXPOSE 8765

# Default command runs the dashboard. The portfolio service in docker-compose
# overrides this with `python -m live.portfolio_runner`.
# sh -c so ${DASHBOARD_HOST} is expanded, exec so uvicorn is PID 1 and gets
# SIGTERM. Default is loopback.
CMD ["sh", "-c", "exec uvicorn dashboard.app:app --host \"${DASHBOARD_HOST:-127.0.0.1}\" --port 8765"]

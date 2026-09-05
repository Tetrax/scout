FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG APP_UID=1000
ARG APP_GID=1000
ARG SCOUT_REVISION=development

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCOUT_REVISION=${SCOUT_REVISION}

LABEL org.opencontainers.image.title="Scout" \
      org.opencontainers.image.description="Bounded personal discovery application" \
      org.opencontainers.image.revision="${SCOUT_REVISION}"

RUN groupadd --gid "${APP_GID}" scout \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /nonexistent --shell /usr/sbin/nologin scout \
    && install -d -o scout -g scout -m 0700 /var/lib/scout

WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir --requirement requirements.lock

COPY --chown=scout:scout scout_mvp ./scout_mvp
COPY --chown=scout:scout scout_web ./scout_web
COPY --chown=scout:scout scripts ./scripts
COPY --chown=scout:scout wsgi.py ./wsgi.py
RUN python -m compileall -q scout_mvp scout_web scripts wsgi.py

USER scout:scout
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "45", "--graceful-timeout", "15", "--keep-alive", "5", "--max-requests", "1000", "--max-requests-jitter", "100", "--limit-request-line", "4094", "--limit-request-fields", "50", "--limit-request-field_size", "4094", "--worker-tmp-dir", "/tmp", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 sentinel
WORKDIR /app
COPY pyproject.toml README.md ./
COPY requirements/container-constraints.txt ./requirements/container-constraints.txt
COPY app ./app
RUN pip install pip==26.2.1 && pip install --constraint requirements/container-constraints.txt .
COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
USER sentinel
EXPOSE 8000
CMD ["python", "-m", "app.main", "serve", "--host", "0.0.0.0", "--port", "8000"]

# Only selected explicitly; the deployed application does not include test
# dependencies or tests. Test PostgreSQL schemas are isolated by the suite.
FROM base AS verification
USER root
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*
RUN pip install --constraint requirements/container-constraints.txt '.[dev]'
COPY tests ./tests
COPY docker-compose.yml Dockerfile ./
COPY scripts/windows ./scripts/windows
USER sentinel
CMD ["python", "-m", "pytest", "tests/integration/test_postgres_live.py", "tests/integration/test_postgres_migrations.py", "tests/integration/test_postgres_backup_restore.py", "tests/integration/test_federal_registry_postgres.py", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime

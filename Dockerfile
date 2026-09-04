FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 sentinel
WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install .
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
RUN pip install '.[dev]'
COPY tests ./tests
USER sentinel
CMD ["python", "-m", "pytest", "tests/integration/test_postgres_live.py", "tests/integration/test_postgres_migrations.py", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime

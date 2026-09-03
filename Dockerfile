FROM python:3.12-slim AS runtime
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


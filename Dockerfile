FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first to benefit from Docker layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src
COPY sql ./sql

# Run as a non-root user; data/ and logs/ are bind-mounted by docker-compose.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data /app/logs \
    && chown -R app:app /app
USER app

CMD ["python", "-m", "src.main", "--load-db"]

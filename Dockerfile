FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TOKENIZERS_PARALLELISM=false HF_HUB_DISABLE_TELEMETRY=1
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && useradd --create-home --uid 10001 app \
    && mkdir /models && chown app:app /models
COPY server ./server
COPY mcp ./mcp
COPY migrations ./migrations
COPY examples ./examples
COPY tests ./tests
USER app
EXPOSE 8000
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]

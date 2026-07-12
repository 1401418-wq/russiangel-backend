FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN groupadd --system --gid 10001 appgroup \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appgroup /app
ENV PYTHONDONTWRITEBYTECODE=1
USER appuser
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]

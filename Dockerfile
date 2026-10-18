FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STORE_AGENT_ENV=production

WORKDIR /app
RUN useradd --create-home --uid 10001 mallops
COPY requirements.prod.txt ./
RUN pip install --no-cache-dir -r requirements.prod.txt
COPY . .
RUN mkdir -p /app/data /app/evidence /app/imports/inbox && chown -R mallops:mallops /app
USER mallops

EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/livez', timeout=3)"

CMD ["python", "app_fastapi.py", "--host", "0.0.0.0", "--port", "8766"]

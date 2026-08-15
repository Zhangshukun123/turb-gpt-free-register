FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ICLOUD_ACCOUNTS_FILE=/data/accounts.json

WORKDIR /app
COPY icloud_gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY icloud_gateway /app/icloud_gateway
RUN mkdir -p /data

EXPOSE 8789
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8789/health', timeout=3).read()"

# One worker keeps the JSON mailbox lease update atomic across requests.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8789", "icloud_gateway.app:app"]

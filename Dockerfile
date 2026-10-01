FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SEAL_DB_PATH=/data/seal.db \
    PORT=8000

WORKDIR /app

# 依赖单独分层，利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY init_db.py ./
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]

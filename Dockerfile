FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
COPY requirements.txt .
RUN /opt/venv/bin/pip install -r requirements.txt


FROM python:3.11-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY . .

RUN adduser --disabled-password --gecos '' appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# 单次交易周期需要串行拉取行情并可能重试模型调用，耗时可达数分钟。
# worker 超时必须留足余量，否则请求会在下单与状态回写之间被杀，
# 留下待对账记录并阻塞后续周期。
CMD ["sh", "-c", "flask --app app:create_app db upgrade && exec gunicorn --workers 1 --threads 8 --timeout 600 --graceful-timeout 120 --bind 0.0.0.0:5000 wsgi:app"]

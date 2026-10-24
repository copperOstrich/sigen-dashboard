FROM python:3.12-alpine
# tzdata so "today" rolls over at local midnight
RUN apk add --no-cache tzdata \
 && pip install --no-cache-dir pillow   # only for the e-ink image (/eink.png); everything else is standard library
WORKDIR /app
COPY collector/ collector/
COPY web/ web/
ENV DATA_DIR=/data \
    HTTP_PORT=8080 \
    TZ=Australia/Sydney \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- "http://127.0.0.1:${HTTP_PORT:-8080}/api/health" || exit 1
CMD ["python", "collector/sigen_collector.py"]

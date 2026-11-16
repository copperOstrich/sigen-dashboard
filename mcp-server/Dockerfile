FROM python:3.12-alpine
RUN apk add --no-cache tzdata \
 && pip install --no-cache-dir "mcp==2.3.*" \
 && adduser -D -H mcp
WORKDIR /app
COPY energy_mcp.py .
USER mcp
ENV MCP_PORT=8000 \
    DASHBOARD_URL=http://sigen-dashboard:8080 \
    TZ=Australia/Sydney \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- "http://127.0.0.1:${MCP_PORT:-8000}/health" || exit 1
CMD ["python", "energy_mcp.py"]

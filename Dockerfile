FROM python:3.12-slim
# Node is needed to launch npx-based MCP servers such as @paypal/mcp
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN grep -v -E '^(pytest|falkordblite|pgserver)' requirements.txt > /tmp/r.txt && pip install --no-cache-dir -r /tmp/r.txt
COPY butler ./butler
CMD ["python", "-m", "butler.telegram_bot"]

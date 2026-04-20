# Slim OAuth-proxy worker — no LiteLLM, no prisma, no npm.
# ~150 MB image vs ~1.5 GB for the v1 worker.
FROM python:3.11-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd -u 1000 -m -s /bin/bash claude
USER claude
WORKDIR /app

COPY --chown=claude:claude oauth_proxy.py ./

ENV CLAUDE_CREDENTIALS_PATH=/home/claude/.claude/.credentials.json \
    ANTHROPIC_UPSTREAM=https://api.anthropic.com \
    CLAUDE_OAUTH_BETA=oauth-2025-04-20 \
    LOG_LEVEL=INFO

EXPOSE 4000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:4000/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "oauth_proxy:app", "--host", "0.0.0.0", "--port", "4000", "--access-log", "--log-level", "info"]

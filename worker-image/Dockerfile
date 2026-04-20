FROM ghcr.io/cabinlab/litellm-claude-code:latest
USER root
RUN npm install -g @anthropic-ai/claude-code@latest && claude --version
# Upgrade LiteLLM past 1.81.10 (chatgpt/gpt-5.4 upstream now requires stream=true)
RUN /opt/venv/bin/pip install --no-cache-dir -U 'litellm[proxy]==1.83.10'
# Phase 2 observability: prometheus_client required for LiteLLM /metrics endpoint
RUN /opt/venv/bin/pip install --no-cache-dir prometheus_client
COPY --chown=claude:claude claude_agent_provider.py /app/config/providers/claude_agent_provider.py
USER claude

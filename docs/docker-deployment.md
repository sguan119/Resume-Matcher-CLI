# Docker Deployment Guide

## Quick Start

### Option A: Use API Key (Recommended for most users)

```bash
# Clone the repository
git clone https://github.com/srbhr/Resume-Matcher.git
cd Resume-Matcher

# Start with docker compose
docker compose up -d

# Open in browser
# http://localhost:3000
```

Then go to **Settings** in the UI and enter your API key (OpenAI, Anthropic, Gemini, etc.).

### Option B: Use Codex CLI (ChatGPT Plus/Pro subscription)

If you have a ChatGPT Plus or Pro subscription, you can use Codex CLI instead of API keys:

```bash
# 1. Install Codex CLI on your host machine
npm install -g @openai/codex

# 2. Login to authenticate (opens browser)
codex login

# 3. Clone and start with Codex volume mounted
git clone https://github.com/srbhr/Resume-Matcher.git
cd Resume-Matcher

# Edit docker-compose.yml to uncomment the codex volume line:
#   - ${HOME}/.codex:/home/appuser/.codex:rw

# 4. Start the container
docker compose up -d

# 5. Fix permissions (Linux/macOS only)
docker exec -u root resume-matcher chown -R 1000:1000 /home/appuser/.codex

# 6. Enable Codex CLI in the UI
# Go to Settings > toggle "Use Codex CLI" ON
```

---

## Configuration

### Environment Variables

Create a `.env` file in the project root (optional):

```env
# Port (default: 3000)
PORT=3000

# LLM Provider: openai, anthropic, openrouter, gemini, deepseek, ollama
LLM_PROVIDER=openai

# API Key (alternative to setting it in the UI)
LLM_API_KEY=sk-your-key-here

# For Ollama on host machine
# LLM_API_BASE=http://host.docker.internal:11434

# Log levels: DEBUG, INFO, WARNING, ERROR
LOG_LEVEL=INFO
LOG_LLM=WARNING
```

### Using Docker Secrets (Production)

```bash
# Create secrets directory
mkdir -p secrets
echo "sk-your-key-here" > secrets/llm_api_key

# Uncomment the secrets section in docker-compose.yml
# Then set: LLM_API_KEY_FILE=/run/secrets/llm_api_key
```

---

## Build from Source

```bash
# Build the image locally
docker build -t resume-matcher .

# Run with custom port
docker run -d \
  --name resume-matcher \
  -p 8080:3000 \
  -v resume-data:/app/backend/data \
  -e LLM_API_KEY=sk-your-key-here \
  resume-matcher
```

### With Codex CLI

```bash
docker run -d \
  --name resume-matcher \
  -p 3000:3000 \
  -v resume-data:/app/backend/data \
  -v ~/.codex:/home/appuser/.codex:rw \
  resume-matcher

# Fix permissions
docker exec -u root resume-matcher chown -R 1000:1000 /home/appuser/.codex
```

---

## Supported LLM Providers

| Provider | Requires API Key | Notes |
|----------|-----------------|-------|
| OpenAI | Yes | GPT-4o, GPT-5, etc. |
| Anthropic | Yes | Claude 3.5, Claude 4 |
| Google Gemini | Yes | Gemini 1.5, 2, 3 |
| OpenRouter | Yes | Access multiple providers |
| DeepSeek | Yes | DeepSeek chat models |
| Ollama | No | Local models, set `LLM_API_BASE` |
| Codex CLI | No (uses ChatGPT subscription) | Toggle in Settings UI |

---

## Troubleshooting

### Health Check

```bash
# Check container health
docker inspect resume-matcher --format '{{.State.Health.Status}}'

# View logs
docker logs resume-matcher

# Test API directly
curl http://localhost:3000/api/v1/health
```

### Codex CLI Permission Denied

If you see `Permission denied (os error 13)` in logs:

```bash
docker exec -u root resume-matcher chown -R 1000:1000 /home/appuser/.codex
```

### Codex CLI Auth Expired

Re-authenticate on the host:

```bash
codex login
# Then restart the container
docker restart resume-matcher
```

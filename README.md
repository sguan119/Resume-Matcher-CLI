# Resume Matcher CLI

> Forked from [srbhr/Resume-Matcher](https://github.com/srbhr/Resume-Matcher). Huge thanks to **[Saurabh Rai](https://github.com/srbhr)** and all contributors for building the original project.

AI-powered resume tailoring that works with your **ChatGPT subscription** — no API keys needed.

This fork replaces API-key-based LLM calls with **CLI-based backends** (starting with OpenAI Codex CLI), so you can use the same subscription you already pay for.

---

## How It Works

1. **Upload** your master resume (PDF or DOCX)
2. **Paste** a job description
3. **Get** AI-tailored resume, cover letter, and outreach email
4. **Export** as professional PDF

All AI processing runs through Codex CLI using your ChatGPT Plus/Pro subscription auth.

---

## Quick Start (Docker)

```bash
# 1. Install and login to Codex CLI
npm install -g @openai/codex
codex login

# 2. Clone and start
git clone https://github.com/sguan119/Resume-Matcher-CLI.git
cd Resume-Matcher-CLI
```

Edit `docker-compose.yml` — uncomment the codex volume line:

```yaml
volumes:
  - resume-data:/app/backend/data
  - ${HOME}/.codex:/home/appuser/.codex:rw  # <-- uncomment this
```

```bash
# 3. Start the container
docker compose up -d

# 4. Fix permissions (Linux/macOS)
docker exec -u root resume-matcher chown -R 1000:1000 /home/appuser/.codex

# 5. Open http://localhost:3000
#    Go to Settings → toggle "Use Codex CLI" ON
```

---

## CLI Backends

| Backend | Status | Auth Method |
|---------|--------|-------------|
| **OpenAI Codex CLI** | Supported | ChatGPT Plus/Pro subscription |
| **Claude CLI** | Planned | Anthropic subscription |
| **Gemini CLI** | Planned | Google subscription |

The goal is to support all major CLI tools so you can use whichever AI subscription you have.

---

## Architecture

```
User → Settings Toggle → llm_dispatch.py → codex_adapter.py → codex exec (subprocess)
                                          → llm.py (LiteLLM)   [fallback / API key mode]
```

- **`llm_dispatch.py`** — Routes `complete()` / `complete_json()` to the active backend
- **`codex_adapter.py`** — Subprocess management, JSON extraction pipeline, repair pass
- **Settings UI** — Toggle between Codex CLI and API key mode at runtime

---

## Local Development

```bash
# Prerequisites: Python 3.13+, Node.js 22+, Codex CLI

# Backend
cd apps/backend
pip install -e .
uvicorn app.main:app --reload --port 8000

# Frontend
cd apps/frontend
npm install
npm run dev

# Open http://localhost:3000
```

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | FastAPI + Python 3.13, LiteLLM / Codex CLI |
| Frontend | Next.js 16 + React 19, Tailwind CSS v4 |
| Database | TinyDB (JSON file storage) |
| PDF Export | Headless Chromium via Playwright |
| i18n | English, Chinese, Spanish, Japanese, Portuguese |

---

## Acknowledgments

This project is a fork of [Resume Matcher](https://github.com/srbhr/Resume-Matcher) by **[Saurabh Rai](https://srbhr.com)**. The original project provides a complete resume tailoring platform with multi-provider LLM support, beautiful Swiss International Style UI, and a vibrant open-source community.

If you find this useful, please also star the [original repo](https://github.com/srbhr/Resume-Matcher).

---

## License

[Apache 2.0](LICENSE) — same as the original project.

# Audio Transcript Bot

Telegram bot that transcribes or summarizes forwarded voice messages. Supports per-user modes (transcript / short / medium / full) and configurable output language (auto / ru / en / es).

## Stack

- **Bot**: `python-telegram-bot` v21 (async)
- **STT**: Groq `whisper-large-v3-turbo` (swappable to local faster-whisper later)
- **LLM**: Gemini 2.5 Flash (swappable to local Ollama / Gemma 3 later)
- **DB**: Neon Postgres via `asyncpg`
- **Host**: Railway (webhook) or anywhere Docker runs (polling)

Monthly cost at ~40 voice msgs/week × 5 min: **~$6/mo** (Railway $5 + Groq ~$0.50 + Gemini ~$1).

## Local dev

```bash
cp .env.example .env
# Fill TELEGRAM_BOT_TOKEN, GROQ_API_KEY, GEMINI_API_KEY, DATABASE_URL
# Leave TELEGRAM_WEBHOOK_BASE empty → polling mode
# Leave ALLOWED_USER_IDS empty initially

python -m venv .venv
source .venv/bin/activate
pip install -e .

python -m app.main
```

Send `/start` to your bot. It will reply with a copy-paste grant command containing your Telegram ID. Copy the ID into `ADMIN_USER_IDS=12345` in `.env` and restart — you're now admin. From then on, add new users at runtime with `/grant <id>` — no redeploy needed.

## Deploy to Railway

1. Push this repo to GitHub.
2. Railway → **New Project → Deploy from GitHub**. Select this repo.
3. Add env vars in Railway's UI:
   - `TELEGRAM_BOT_TOKEN`
   - `ADMIN_USER_IDS` (your Telegram user ID, comma-separated)
   - `DATABASE_URL` (Neon connection string)
   - `GROQ_API_KEY`
   - `GEMINI_API_KEY`
   - `TELEGRAM_WEBHOOK_SECRET` — a long random string (e.g. `openssl rand -hex 32`)
   - `TELEGRAM_WEBHOOK_BASE` — set this **after** Railway assigns a public URL (Settings → Networking → Generate Domain). Value looks like `https://audio-transcript-bot-production.up.railway.app`.
4. Redeploy. Logs should show `Starting in WEBHOOK mode at ...`.

Railway injects `PORT` automatically; the app honors it.

## Switching to local providers (later)

On Mac Mini M4, install Ollama and pull Gemma 3:

```bash
brew install ollama
ollama serve &
ollama pull gemma3:12b
```

Then set in `.env`:

```
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma3:12b
```

Local Whisper provider is stubbed in [app/providers/stt.py](app/providers/stt.py) — implement when ready (`faster-whisper` with `large-v3` on M4 runs ~5-8× realtime).

## Bot commands

- `/start` — onboard / show current settings (unauthorized users get a copy-paste `/grant <id>` command to forward to an admin)
- `/mode transcript|short|medium|full` — default `full`
- `/lang auto|ru|en|es` — `auto` = summary in same language as transcript
- `/settings` — show current settings
- Forward or send a voice/audio message — apply settings, reply with result

**Admin-only:**
- `/grant <telegram_user_id>` — allow a user
- `/revoke <telegram_user_id>` — remove access (admins cannot be revoked this way)
- `/users` — list all allowed users

## Roadmap

- **Phase 1 (done)**: Telegram, admin + runtime allowlist via `/grant`, Groq + Gemini.
- **Phase 1.5**: Local Whisper + Gemma 3 via Ollama on Mac Mini.
- **Phase 2**: WhatsApp — blocked on Business API (requires registered business entity).
- **Phase 3**: Voice-to-voice translation (TTS on top of translated summary).

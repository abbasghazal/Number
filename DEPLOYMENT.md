# Deployment

This bot can run as a web service on Render and similar hosts. The app starts
the Telegram bot and a small HTTP health server on `0.0.0.0:$PORT`.

## Required environment variables

- `API_ID`
- `API_HASH`
- `ADMIN_ID`
- `BOT_TOKEN`
- `SESSION_ENCRYPTION_KEY`

Generate `SESSION_ENCRYPTION_KEY` with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Optional environment variables

- `PORT`: hosting platforms usually set this automatically. Render defaults to `10000`.
- `HOST`: defaults to `0.0.0.0`.
- `DB_PATH`: defaults to `database/KingA.db`.

## Render

Use these settings for a normal Python web service:

- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`

The repository also includes `render.yaml` if you prefer creating the service
from a Blueprint.

Important: Render's free filesystem is ephemeral. For production, attach a
persistent disk mounted at the project `database` directory, or move the bot
state to an external database. Otherwise data can disappear after redeploys.

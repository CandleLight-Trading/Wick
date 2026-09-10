"""Secrets and webhooks come from the environment or backend/.env (gitignored). Never from code."""
import os

from . import config


def load_env() -> dict[str, str]:
    """os.environ overlaid on KEY=VALUE lines from .env. A bare 'sk-...' line is taken as the
    OpenAI key, because that is what people paste."""
    out: dict[str, str] = {}
    try:
        for line in config.ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
            elif line.startswith("sk-"):
                out["OPENAI_API_KEY"] = line.strip('"').strip("'")
    except FileNotFoundError:
        pass
    for k in ("OPENAI_API_KEY", "DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "WICK_PASSWORD", "WICK_SESSION_SECRET", "WICK_ENV", "DATABASE_PATH", "PORT",
              "CLERK_SECRET_KEY", "CLERK_AUTHORIZED_PARTIES", "CLERK_JASON_USER_ID"):
        if os.environ.get(k):
            out[k] = os.environ[k].strip()
    return out


def is_production(env: dict[str, str]) -> bool:
    return env.get("WICK_ENV", "").lower() in ("production", "prod")

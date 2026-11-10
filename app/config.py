"""Configuration lue depuis l'environnement (fichier .env via docker-compose ou systemd)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'sharedfleet.db'}")
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Paris"))
ALLOW_SIGNUP = os.environ.get("ALLOW_SIGNUP", "true").lower() == "true"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
# Désactive les tâches de fond (tests)
DISABLE_SCHEDULER = os.environ.get("DISABLE_SCHEDULER", "false").lower() == "true"

# Minutes avant le début de location où le code d'accès est envoyé par SMS
SMS_LEAD_MINUTES = int(os.environ.get("SMS_LEAD_MINUTES", "30"))
# Heures après la fin prévue pendant lesquelles le code client reste valable (retour en retard)
ACCESS_GRACE_HOURS = int(os.environ.get("ACCESS_GRACE_HOURS", "12"))


def org_dir(org_id: int, *parts: str) -> Path:
    p = DATA_DIR / f"org_{org_id}"
    for part in parts:
        p = p / part
    p.mkdir(parents=True, exist_ok=True)
    return p

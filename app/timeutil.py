"""Toutes les dates sont stockées en heure locale « naïve » (fuseau de config.TZ, Europe/Paris par défaut)."""
from datetime import datetime

from . import config


def now() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None, microsecond=0)


def to_local(value: str | datetime | None) -> datetime | None:
    """ISO 8601 (avec ou sans décalage) → datetime naïf en heure locale."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if dt.tzinfo is not None:
        dt = dt.astimezone(config.TZ).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    """datetime naïf local → ISO 8601 avec décalage (pour les API externes)."""
    return dt.replace(tzinfo=config.TZ).isoformat()

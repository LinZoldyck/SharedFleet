"""Mots de passe, session, rôles, protection CSRF et limitation des tentatives."""
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .models import User

ROLE_LEVEL = {"viewer": 1, "manager": 2, "admin": 3}


# --- mots de passe (scrypt, bibliothèque standard) ---
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, AttributeError):
        return False


# --- chiffrement des jetons d'API stockés en base ---
def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode()).digest()))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return ""  # SECRET_KEY changée : le jeton doit être ressaisi


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- authentification ---
class LoginRequired(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.active:
        request.session.pop("uid", None)
        raise LoginRequired()
    request.state.user = user
    return user


def require_role(minimum: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_LEVEL[user.role] < ROLE_LEVEL[minimum]:
            raise HTTPException(403, "Votre rôle ne permet pas cette action.")
        return user

    return dep


manager = require_role("manager")
admin = require_role("admin")


def api_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Authentification par jeton (extension Chrome) : Authorization: Bearer <jeton>."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Jeton manquant")
    user = db.query(User).filter_by(api_token_hash=token_hash(auth[7:].strip()), active=True).first()
    if not user or ROLE_LEVEL[user.role] < ROLE_LEVEL["manager"]:
        raise HTTPException(401, "Jeton invalide")
    return user


# --- CSRF : les formulaires doivent venir du même site (en plus du cookie SameSite=Lax) ---
CSRF_EXEMPT = ("/webhooks/", "/api/ext/")


def same_origin(request: Request) -> bool:
    if request.method in ("GET", "HEAD", "OPTIONS") or request.url.path.startswith(CSRF_EXEMPT):
        return True
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    return urlparse(source).netloc == request.headers.get("host")


# --- limitation des tentatives (connexion, code client) ---
_attempts: dict[str, list[float]] = defaultdict(list)


def too_many_attempts(key: str, limit: int = 8, window: int = 600) -> bool:
    """Enregistre une tentative ; True si la limite est dépassée sur la fenêtre (secondes)."""
    t = time.time()
    hits = [h for h in _attempts[key] if h > t - window]
    hits.append(t)
    _attempts[key] = hits
    return len(hits) > limit


def clear_attempts(key: str) -> None:
    _attempts.pop(key, None)


def client_ip(request: Request) -> str:
    """Adresse du client. Derrière un proxy inverse, uvicorn la rétablit depuis X-Forwarded-For
    uniquement pour les proxys de confiance (option --proxy-headers et variable FORWARDED_ALLOW_IPS)."""
    return request.client.host if request.client else "?"

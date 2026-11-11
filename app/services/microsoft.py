"""Connexion OAuth 2.0 à Microsoft pour envoyer des e-mails depuis Outlook.com / Hotmail (ou Microsoft 365).

Microsoft n'accepte plus le mot de passe pour l'envoi SMTP depuis un compte Outlook.com. L'application obtient
donc un jeton OAuth :
1. « connexion par code » (device code flow) : l'application demande un code à Microsoft, l'utilisateur le saisit
   sur microsoft.com/devicelogin depuis n'importe quel appareil et accepte. Aucune adresse de retour n'est
   nécessaire : cela fonctionne avec un Raspberry joignable uniquement en http:// sur le réseau local ;
2. Microsoft délivre un jeton d'accès (≈ 1 h) et un jeton de renouvellement, conservé chiffré en base ;
3. à chaque envoi, le jeton d'accès sert à s'identifier auprès du serveur SMTP (mécanisme XOAUTH2).

Pré-requis : une « inscription d'application » dans le portail Microsoft Entra (identifiant client), voir README.
"""
import base64
import json
import threading
import time

import httpx
from sqlalchemy.orm.attributes import set_committed_value

from ..models import Organization
from . import settings

SCOPE = "https://outlook.office.com/SMTP.Send offline_access openid profile"
SMTP_HOSTS = {"consumers": "smtp-mail.outlook.com"}  # comptes personnels ; Microsoft 365 : smtp.office365.com
DEFAULT_HOST = "smtp.office365.com"

_transport: httpx.BaseTransport | None = None  # remplacé dans les tests
_lock = threading.Lock()
_access: dict[int, tuple[str, float]] = {}  # org_id → (jeton d'accès, expiration) — en mémoire seulement
_pending: dict[int, dict] = {}  # org_id → connexion par code en cours


class MicrosoftError(Exception):
    pass


def tenant(org: Organization) -> str:
    return (settings.get(org, "ms_tenant") or "consumers").strip()


def smtp_host(org: Organization) -> str:
    return SMTP_HOSTS.get(tenant(org), DEFAULT_HOST)


def connected(org: Organization) -> bool:
    return settings.has(org, "ms_refresh_token")


def _post(org: Organization, endpoint: str, data: dict) -> tuple[int, dict]:
    url = f"https://login.microsoftonline.com/{tenant(org)}/oauth2/v2.0/{endpoint}"
    try:
        with httpx.Client(transport=_transport, timeout=20) as http:
            r = http.post(url, data={"client_id": settings.get(org, "ms_client_id").strip(), **data})
        return r.status_code, r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MicrosoftError(f"Microsoft injoignable : {exc}") from exc


def _account_from_id_token(id_token: str) -> str:
    """Adresse du compte, lue dans le jeton d'identité reçu directement de Microsoft (via HTTPS)."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("email") or claims.get("preferred_username") or ""
    except (IndexError, ValueError):
        return ""


def _save(org: Organization, values: dict) -> None:
    """Enregistre les jetons immédiatement, dans une session dédiée (le renouvellement peut survenir n'importe où)."""
    from ..db import SessionLocal

    with SessionLocal() as db:
        fresh = db.get(Organization, org.id)
        settings.update(fresh, values)
        db.commit()
        set_committed_value(org, "settings", dict(fresh.settings))


# --- connexion par code ---
def start_device_login(org: Organization) -> dict:
    if not settings.get(org, "ms_client_id").strip():
        raise MicrosoftError("Renseignez d'abord l'identifiant d'application (client ID) Microsoft.")
    status, data = _post(org, "devicecode", {"scope": SCOPE})
    if status != 200 or "device_code" not in data:
        raise MicrosoftError(_explain(data))
    _pending[org.id] = {"device_code": data["device_code"], "expires": time.time() + int(data.get("expires_in", 900)),
                        "interval": int(data.get("interval", 5))}
    return {"user_code": data["user_code"], "verification_uri": data.get("verification_uri", "https://microsoft.com/devicelogin"),
            "interval": int(data.get("interval", 5)), "expires_in": int(data.get("expires_in", 900))}


def poll_device_login(org: Organization) -> dict:
    """Interroge Microsoft une fois. Retourne {"status": "pending" | "ok" | "error", ...}."""
    pending = _pending.get(org.id)
    if not pending or time.time() > pending["expires"]:
        _pending.pop(org.id, None)
        return {"status": "error", "message": "Le code a expiré. Recommencez la connexion."}
    status, data = _post(org, "token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                        "device_code": pending["device_code"]})
    error = data.get("error")
    if error in ("authorization_pending", "slow_down"):
        return {"status": "pending"}
    _pending.pop(org.id, None)
    if status != 200 or "refresh_token" not in data:
        return {"status": "error", "message": _explain(data)}
    account = _account_from_id_token(data.get("id_token", "")) or settings.get(org, "email_from")
    _access[org.id] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 120)
    _save(org, {"ms_refresh_token": data["refresh_token"], "ms_account": account, "ms_error": ""})
    return {"status": "ok", "account": account}


def disconnect(org: Organization) -> None:
    _access.pop(org.id, None)
    _pending.pop(org.id, None)
    _save(org, {"ms_refresh_token": "-", "ms_account": "", "ms_error": ""})


# --- jeton d'accès ---
def access_token(org: Organization, force: bool = False) -> str:
    """Jeton d'accès valide, renouvelé si besoin (le jeton de renouvellement tourne : on garde le nouveau)."""
    with _lock:
        cached = _access.get(org.id)
        if cached and cached[1] > time.time() and not force:
            return cached[0]
        refresh = settings.get(org, "ms_refresh_token")
        if not refresh:
            raise MicrosoftError("Compte Microsoft non connecté (Réglages → E-mail → Se connecter avec Microsoft).")
        status, data = _post(org, "token", {"grant_type": "refresh_token", "refresh_token": refresh, "scope": SCOPE})
        if status != 200 or "access_token" not in data:
            message = _explain(data)
            if data.get("error") in ("invalid_grant", "interaction_required"):
                message = "La connexion Microsoft a expiré ou a été révoquée : reconnectez le compte dans les réglages."
            _save(org, {"ms_error": message})
            raise MicrosoftError(message)
        _access[org.id] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 120)
        _save(org, {"ms_refresh_token": data.get("refresh_token") or refresh, "ms_error": ""})
        return data["access_token"]


def xoauth2(user: str, token: str) -> str:
    """Chaîne d'identification SASL XOAUTH2 (encodée en base64 par smtplib)."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


def keep_alive(org: Organization) -> None:
    """Renouvelle le jeton même sans envoi : un jeton de renouvellement inutilisé finit par expirer."""
    if settings.get(org, "email_provider") == "microsoft" and connected(org):
        access_token(org, force=True)


def _explain(data: dict) -> str:
    error, description = data.get("error", ""), (data.get("error_description") or "").split("\r\n")[0]
    hints = {
        "invalid_client": "Identifiant d'application inconnu ou mal configuré.",
        "unauthorized_client": "L'application Microsoft n'autorise pas ce type de compte : choisissez « comptes Microsoft "
                               "personnels » dans l'inscription d'application.",
        "authorization_declined": "Connexion refusée sur la page Microsoft.",
        "expired_token": "Le code a expiré. Recommencez la connexion.",
    }
    if "AADSTS7000218" in description or "public client" in description.lower():
        return "Activez « Autoriser les flux de clients publics » dans l'inscription d'application Microsoft (onglet Authentification)."
    return hints.get(error) or (f"Microsoft : {description}" if description else f"Erreur Microsoft ({error or 'inconnue'}).")

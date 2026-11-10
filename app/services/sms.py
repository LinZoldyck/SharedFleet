"""Envoi de SMS. Fournisseurs : console (journal, pour tester), Twilio, ou passerelle HTTP générique."""
import logging

import httpx

from ..models import Organization
from . import settings

log = logging.getLogger("sharedfleet.sms")


def normalize_phone(phone: str) -> str:
    """06 12 34 56 78 → +33612345678"""
    digits = "".join(c for c in phone if c.isdigit() or c == "+")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0"):
        digits = "+33" + digits[1:]
    return digits


def send_sms(org: Organization, to: str, text: str) -> bool:
    to = normalize_phone(to)
    provider = settings.get(org, "sms_provider")
    try:
        if provider == "twilio":
            sid = settings.get(org, "twilio_sid")
            r = httpx.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=(sid, settings.get(org, "twilio_token")),
                data={"To": to, "From": settings.get(org, "twilio_from"), "Body": text}, timeout=20)
            r.raise_for_status()
        elif provider == "http":
            # Passerelle générique : POST JSON {to, message} avec jeton Bearer facultatif
            token = settings.get(org, "sms_http_token")
            r = httpx.post(settings.get(org, "sms_http_url"), json={"to": to, "message": text},
                           headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=20)
            r.raise_for_status()
        else:
            log.warning("SMS (mode console, non envoyé) → %s : %s", to, text)
        return True
    except httpx.HTTPError as exc:
        log.error("Échec d'envoi du SMS à %s : %s", to, exc)
        return False

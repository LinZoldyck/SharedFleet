"""Règles métier des locations directes : conflits de planning, code d'accès client, SMS."""
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import config
from ..models import Booking, EventLog, Organization
from ..timeutil import now
from . import settings
from .sms import send_sms

ACTIVE = ("confirmed", "in_progress")


def conflicts(db: Session, org_id: int, vehicle_id: int, start: datetime, end: datetime,
              exclude_id: int | None = None) -> list[Booking]:
    q = db.query(Booking).filter(Booking.org_id == org_id, Booking.vehicle_id == vehicle_id,
                                 Booking.status != "cancelled", Booking.starts_at < end, Booking.ends_at > start)
    if exclude_id:
        q = q.filter(Booking.id != exclude_id)
    return q.all()


def new_access_code(db: Session) -> str:
    """Code aléatoire à 6 chiffres, unique parmi les locations encore actives."""
    while True:
        code = f"{secrets.randbelow(10**6):06d}"
        if not db.query(Booking).filter(Booking.access_code == code, Booking.status.in_(ACTIVE)).first():
            return code


def _code_email(org: Organization, booking: Booking):
    """E-mail du code d'accès. Le code du Master Lock n'y figure jamais : il n'est montré qu'une fois le code saisi."""
    from html import escape

    from . import email

    v, c = booking.vehicle, booking.customer
    when = f"{booking.starts_at:%d/%m/%Y à %Hh%M}"
    url = f"{config.BASE_URL}/acces"
    hello = f"Bonjour {c.first_name}," if c.first_name else "Bonjour,"
    lines = [hello, "", f"Votre location du {v.brand} {v.model} ({v.plate}) commence le {when}.".replace("  ", " "), "",
             f"Votre code d'accès : {booking.access_code}", "",
             f"1. Ouvrez {url} sur votre téléphone et saisissez ce code.",
             "2. Le code de la boîte à clés s'affiche : récupérez les clés.",
             "3. Faites l'état des lieux de départ (photos, kilométrage, carburant).", ""]
    if v.address:
        lines += [f"Adresse de prise en charge : {v.address}", ""]
    lines += [f"Retour prévu le {booking.ends_at:%d/%m/%Y à %Hh%M}.", f"Le code est valable à partir de {config.SMS_LEAD_MINUTES} minutes avant le départ.",
              "", "Bonne route,", settings.get(org, "company_name") or org.name]
    e = escape
    html = f"""<!doctype html><html lang="fr"><body style="margin:0;background:#f6f7fb;font-family:Arial,Helvetica,sans-serif;color:#0f172a">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border:1px solid #e6e8ef;border-radius:14px">
<tr><td style="padding:24px 24px 8px">{{{{logo}}}}<div style="font-weight:bold;font-size:15px;margin-top:6px">{e(settings.get(org, "company_name") or org.name)}</div></td></tr>
<tr><td style="padding:8px 24px">
<p style="margin:0 0 12px">{e(hello)}</p>
<p style="margin:0 0 16px">Votre location du <b>{e(f"{v.brand} {v.model}".strip() or v.name)}</b> ({e(v.plate)}) commence le <b>{e(when)}</b>.</p>
<div style="font-size:13px;color:#64748b;margin-bottom:6px">Votre code d'accès</div>
<div style="font-family:Courier New,monospace;font-size:34px;font-weight:bold;letter-spacing:8px;text-align:center;background:#eef2ff;color:#4f46e5;border-radius:12px;padding:16px 8px">{e(booking.access_code)}</div>
<p style="text-align:center;margin:20px 0"><a href="{e(url)}" style="background:#4f46e5;color:#ffffff;text-decoration:none;font-weight:bold;padding:13px 22px;border-radius:12px;display:inline-block">Accéder à ma location</a></p>
<ol style="padding-left:20px;margin:0 0 16px;line-height:1.6">
<li>Saisissez le code sur la page de votre location.</li><li>Le code de la boîte à clés s'affiche : récupérez les clés.</li>
<li>Faites l'état des lieux de départ (photos, kilométrage, carburant).</li></ol>
{f'<p style="margin:0 0 8px"><b>Adresse :</b> {e(v.address)}</p>' if v.address else ''}
<p style="margin:0 0 8px"><b>Retour prévu :</b> {e(f"{booking.ends_at:%d/%m/%Y à %Hh%M}")}</p>
</td></tr>
<tr><td style="padding:12px 24px 24px;font-size:12px;color:#64748b">Le code est valable à partir de {config.SMS_LEAD_MINUTES} minutes avant le départ. Ne le partagez pas.</td></tr>
</table></td></tr></table></body></html>"""
    return email.build(org, c.email, c.person, f"Votre code d'accès — location du {when}", "\n".join(lines), html)


def send_access_code(db: Session, org: Organization, booking: Booking, channels: tuple[str, ...] | None = None) -> dict[str, str]:
    """Envoie le code par SMS et/ou e-mail selon les réglages et les coordonnées du client.

    Retourne {canal: "ok" | raison de l'échec}. Un canal non configuré ou sans coordonnée est absent du résultat.
    La location est marquée « code envoyé » dès qu'un canal a réussi.
    """
    from . import email

    c = booking.customer
    if not c:
        return {}
    if channels is None:
        channels = tuple(ch for ch in ("sms", "email") if settings.get(org, f"code_by_{ch}"))
    if not booking.access_code:
        booking.access_code = new_access_code(db)
    results: dict[str, str] = {}
    if "sms" in channels and c.phone:
        text = (f"{org.name} : votre location du {booking.vehicle.name} commence le "
                f"{booking.starts_at:%d/%m à %Hh%M}. Code d'accès : {booking.access_code}. "
                f"Récupérez les clés et faites l'état des lieux ici : {config.BASE_URL}/acces")
        results["sms"] = "ok" if send_sms(org, c.phone, text) else "échec de l'envoi"
    if "email" in channels and email.enabled(org) and email.looks_valid(c.email):
        try:
            email.send(org, _code_email(org, booking))
            results["email"] = "ok"
        except email.EmailError as exc:
            results["email"] = str(exc)
    for channel, outcome in results.items():
        db.add(EventLog(org_id=org.id, kind="access_code", key=f"booking:{booking.id}",
                        detail=f"{channel}|{outcome}|{c.phone if channel == 'sms' else c.email}"))
    if "ok" in results.values():
        booking.code_sent_at = now()
    db.commit()
    return results


def code_history(db: Session, booking: Booking) -> list[dict]:
    """Historique des envois du code (le plus récent d'abord)."""
    rows = db.query(EventLog).filter_by(org_id=booking.org_id, kind="access_code", key=f"booking:{booking.id}") \
        .order_by(EventLog.id.desc()).limit(10).all()
    out = []
    for r in rows:
        channel, outcome, target = (r.detail.split("|", 2) + ["", ""])[:3]
        out.append({"channel": channel, "ok": outcome == "ok", "error": "" if outcome == "ok" else outcome, "to": target, "at": r.created_at})
    return out


def due_for_code(db: Session) -> list[Booking]:
    """Locations directes dont le début est dans moins de SMS_LEAD_MINUTES et dont le code n'est pas parti."""
    t = now()
    return db.query(Booking).filter(
        Booking.source == "direct", Booking.status == "confirmed", Booking.code_sent_at.is_(None),
        Booking.starts_at <= t + timedelta(minutes=config.SMS_LEAD_MINUTES), Booking.ends_at > t).all()


def find_by_code(db: Session, code: str) -> Booking | None:
    """Location correspondant au code saisi par le client, si le code est dans sa fenêtre de validité."""
    code = "".join(c for c in code if c.isdigit())
    if len(code) != 6:
        return None
    b = db.query(Booking).filter(Booking.access_code == code, Booking.source == "direct",
                                 Booking.status.in_(ACTIVE)).first()
    if not b:
        return None
    t = now()
    opens = b.starts_at - timedelta(minutes=config.SMS_LEAD_MINUTES)
    closes = b.ends_at + timedelta(hours=config.ACCESS_GRACE_HOURS)
    return b if opens <= t <= closes else None


def save_inspection(db: Session, booking: Booking, kind: str, mileage: int | None, fuel_level: int | None,
                    notes: str, exterior: list[bytes], interior: list[bytes]):
    """Enregistre un état des lieux (départ ou retour) avec ses photos et fait avancer le statut de la location."""
    from ..models import Inspection, Photo
    from . import media

    inspection = booking.inspection(kind)
    if not inspection:
        inspection = Inspection(booking=booking, kind=kind)
        db.add(inspection)
    inspection.mileage, inspection.fuel_level, inspection.notes = mileage, fuel_level, notes.strip()
    inspection.created_at = now()
    for category, files in (("exterior", exterior), ("interior", interior)):
        for data in files:
            path = media.save_image(booking.org_id, f"photos/location_{booking.id}", data)
            inspection.photos.append(Photo(category=category, path=path))
    booking.status = "in_progress" if kind == "checkin" else "completed"
    db.commit()
    return inspection

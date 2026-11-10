"""Règles métier des locations directes : conflits de planning, code d'accès client, SMS."""
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import config
from ..models import Booking, Organization
from ..timeutil import now
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


def send_access_code(db: Session, org: Organization, booking: Booking) -> bool:
    if not booking.customer or not booking.customer.phone:
        return False
    if not booking.access_code:
        booking.access_code = new_access_code(db)
    text = (f"{org.name} : votre location du {booking.vehicle.name} commence le "
            f"{booking.starts_at:%d/%m à %Hh%M}. Code d'accès : {booking.access_code}. "
            f"Récupérez les clés et faites l'état des lieux ici : {config.BASE_URL}/acces")
    ok = send_sms(org, booking.customer.phone, text)
    if ok:
        booking.code_sent_at = now()
    db.commit()
    return ok


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

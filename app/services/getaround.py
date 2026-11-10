"""Intégration de l'API propriétaire Getaround (Owner API v1).

Source : spécification OpenAPI publiée par Getaround (https://api-eu.getaround.com/owner/v1).
L'API permet de lire voitures, locations, conducteurs, états des lieux, factures, d'échanger des messages
et de poser des indisponibilités. Elle NE permet PAS de déverrouiller un véhicule : l'ouverture Getaround
Connect se fait uniquement dans l'application Getaround du locataire.
"""
import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from ..models import Booking, Customer, EventLog, Message, Organization, Vehicle
from ..timeutil import now, to_iso, to_local
from . import settings

log = logging.getLogger("sharedfleet.getaround")
BASE_URL = "https://api-eu.getaround.com/owner/v1"


class GetaroundError(Exception):
    pass


class GetaroundClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=BASE_URL, timeout=30, transport=transport, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, url: str, **kw):
        try:
            r = self.http.request(method, url, **kw)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GetaroundError(f"Getaround a répondu {exc.response.status_code} sur {url}") from exc
        except httpx.HTTPError as exc:
            raise GetaroundError(f"Getaround injoignable : {exc}") from exc
        return r

    def _list(self, path: str, params: dict | None = None) -> list[dict]:
        """Suit la pagination par en-tête Link (RFC 8288), comme demandé par la documentation."""
        items, url, params = [], path, {**(params or {}), "per_page": 50}
        while url:
            r = self._request("GET", url, params=params)
            items += r.json()
            url, params = r.links.get("next", {}).get("url"), None
        return items

    def cars(self) -> list[dict]:
        return [self._request("GET", f"/cars/{c['id']}.json").json() for c in self._list("/cars.json")]

    def rental_ids(self, start: datetime, end: datetime) -> list[int]:
        """Locations réservées entre deux dates. L'API refuse les fenêtres de plus de 30 jours : on découpe."""
        ids: list[int] = []
        while start < end:
            stop = min(end, start + timedelta(days=30))
            ids += [r["id"] for r in self._list("/rentals.json", {"start_date": to_iso(start), "end_date": to_iso(stop)})]
            start = stop
        return list(dict.fromkeys(ids))

    def rental(self, rental_id) -> dict:
        return self._request("GET", f"/rentals/{rental_id}.json").json()

    def user(self, user_id) -> dict:
        return self._request("GET", f"/users/{user_id}.json").json()

    def message_ids(self, rental_id) -> list[int]:
        return [m["id"] for m in self._list(f"/rentals/{rental_id}/messages.json")]

    def message(self, rental_id, message_id) -> dict:
        return self._request("GET", f"/rentals/{rental_id}/messages/{message_id}.json").json()

    def send_message(self, rental_id, content: str) -> dict:
        r = self._request("POST", f"/rentals/{rental_id}/messages.json", json={"content": content[:2000]})
        return r.json() if r.content else {}

    def create_unavailability(self, car_id, start: datetime, end: datetime, reason: str = "booked") -> None:
        self._request("POST", f"/cars/{car_id}/unavailabilities.json",
                      json={"starts_at": to_iso(_floor30(start)), "ends_at": to_iso(_ceil30(end)), "reason": reason})

    def delete_unavailability(self, car_id, start: datetime, end: datetime) -> None:
        self._request("DELETE", f"/cars/{car_id}/unavailabilities.json",
                      json={"starts_at": to_iso(_floor30(start)), "ends_at": to_iso(_ceil30(end))})


def _floor30(dt: datetime) -> datetime:  # l'API impose une granularité de 30 minutes
    return dt.replace(minute=dt.minute - dt.minute % 30, second=0, microsecond=0)


def _ceil30(dt: datetime) -> datetime:
    floor = _floor30(dt)
    return floor if floor == dt.replace(second=0, microsecond=0) else floor + timedelta(minutes=30)


def client_for(org: Organization) -> GetaroundClient | None:
    token = settings.get(org, "getaround_token")
    return GetaroundClient(token) if token else None


def normalize_plate(plate: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (plate or "").upper())


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """En-tête X-Drivy-Signature : « sha1= » + HMAC-SHA1 hexadécimal du corps brut."""
    if not secret or not header:
        return False
    expected = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, header)


# --- synchronisation ---
def sync_cars(db: Session, org: Organization, api: GetaroundClient) -> int:
    """Rattache les voitures Getaround aux véhicules de la flotte par plaque d'immatriculation."""
    linked = 0
    vehicles = {normalize_plate(v.plate): v for v in db.query(Vehicle).filter_by(org_id=org.id)}
    for car in api.cars():
        v = vehicles.get(normalize_plate(car.get("plate_number", "")))
        if v and v.getaround_car_id != car["id"]:
            v.getaround_car_id = car["id"]
            linked += 1
    db.commit()
    return linked


def _upsert_driver(db: Session, org: Organization, api: GetaroundClient, user_id: int) -> Customer:
    """Le conducteur Getaround devient un client : ses coordonnées servent à la désignation en cas d'amende."""
    c = db.query(Customer).filter_by(org_id=org.id, getaround_user_id=user_id).first()
    if not c:
        c = Customer(org_id=org.id, getaround_user_id=user_id)
        db.add(c)
    u = api.user(user_id)
    c.first_name, c.last_name = u.get("first_name") or "", u.get("last_name") or ""
    c.phone = u.get("phone_number") or ""
    c.address = " ".join(x for x in (u.get("address_line1"), u.get("address_line2")) if x)
    c.postal_code, c.city = u.get("postal_code") or "", u.get("city") or ""
    c.country = u.get("country") or "France"
    c.license_number = u.get("license_number") or ""
    c.license_country = u.get("license_country") or ""
    for field, key in (("birth_date", "birth_date"), ("license_date", "license_first_issue_date")):
        if u.get(key):
            setattr(c, field, datetime.fromisoformat(u[key]).date())
    return c


def sync_rental(db: Session, org: Organization, api: GetaroundClient, rental_id, canceled: bool = False) -> Booking | None:
    booking = db.query(Booking).filter_by(org_id=org.id, source="getaround", external_id=str(rental_id)).first()
    if canceled:
        if booking:
            booking.status = "cancelled"
            db.commit()
        return booking
    r = api.rental(rental_id)
    find = lambda: db.query(Vehicle).filter_by(org_id=org.id, getaround_car_id=r["car_id"]).first()  # noqa: E731
    vehicle = find()
    if not vehicle:  # voiture pas encore rattachée (premier webhook avant toute synchronisation) : on rattache puis on réessaie
        sync_cars(db, org, api)
        vehicle = find()
    if not vehicle:
        log.warning("Location Getaround %s ignorée : voiture %s non rattachée à un véhicule", rental_id, r["car_id"])
        return None
    if not booking:
        booking = Booking(org_id=org.id, source="getaround", external_id=str(rental_id), vat_rate=0)
        db.add(booking)
    booking.vehicle_id = vehicle.id
    booking.starts_at, booking.ends_at = to_local(r["starts_at"]), to_local(r["ends_at"])
    cents = (r.get("price") or 0) + ((r.get("insurance_fee") or 0) if settings.get(org, "getaround_include_insurance") else 0)
    booking.price_ht = cents / 100  # l'API exprime les montants en centimes
    try:
        booking.customer = _upsert_driver(db, org, api, r["user_id"])
    except GetaroundError as exc:
        log.warning("Conducteur %s illisible : %s", r.get("user_id"), exc)
    t = now()
    if booking.status != "cancelled":
        booking.status = "completed" if booking.ends_at < t else "in_progress" if booking.starts_at <= t else "confirmed"
    db.commit()
    return booking


def sync_messages(db: Session, org: Organization, api: GetaroundClient, booking: Booking) -> list[Message]:
    """Importe les messages d'une location. Retourne les nouveaux messages reçus du locataire."""
    known = {m.external_id for m in db.query(Message).filter_by(org_id=org.id, booking_id=booking.id)}
    driver_id = booking.customer.getaround_user_id if booking.customer else None
    fresh = []
    for mid in api.message_ids(booking.external_id):
        if str(mid) in known:
            continue
        m = api.message(booking.external_id, mid)
        incoming = m.get("sending_user_id") == driver_id
        msg = Message(org_id=org.id, booking_id=booking.id, external_id=str(mid), content=m.get("content") or "",
                      direction="in" if incoming else "out", status="new" if incoming else "sent",
                      sent_at=to_local(m.get("sent_at")) or now())
        db.add(msg)
        if incoming:
            fresh.append(msg)
    db.commit()
    return fresh


def sync_all(db: Session, org: Organization, days_back: int = 30, days_ahead: int = 60) -> dict:
    """Synchronisation complète : voitures, locations de la fenêtre, messages des locations récentes."""
    api = client_for(org)
    if not api:
        raise GetaroundError("Jeton Getaround non configuré.")
    from .assistant import handle_incoming  # import tardif : évite la dépendance circulaire

    stats = {"cars": sync_cars(db, org, api), "rentals": 0, "messages": 0}
    t = now()
    for rid in api.rental_ids(t - timedelta(days=days_back), t + timedelta(days=days_ahead)):
        if sync_rental(db, org, api, rid):
            stats["rentals"] += 1
    # Messages : seulement les locations à venir, en cours, ou terminées depuis moins de 7 jours
    recent = db.query(Booking).filter(Booking.org_id == org.id, Booking.source == "getaround",
                                      Booking.status != "cancelled", Booking.ends_at > t - timedelta(days=7))
    for booking in recent:
        for msg in sync_messages(db, org, api, booking):
            stats["messages"] += 1
            handle_incoming(db, org, api, msg)
    return stats


def handle_webhook(db: Session, org: Organization, event: dict) -> str:
    """Traite un événement webhook de façon idempotente (Getaround peut renvoyer un même événement)."""
    kind, data = event.get("type", ""), event.get("data") or {}
    key = f"{kind}:{data.get('rental_id')}:{data.get('message_id')}:{event.get('occurred_at')}"
    if db.query(EventLog).filter_by(org_id=org.id, kind="getaround", key=key).first():
        return "duplicate"
    db.add(EventLog(org_id=org.id, kind="getaround", key=key))
    db.commit()
    api = client_for(org)
    if kind == "ping" or not api:
        return "ok"
    from .assistant import handle_incoming

    if kind.startswith("rental."):
        sync_rental(db, org, api, data["rental_id"], canceled=kind == "rental.canceled")
    elif kind == "message.sent":
        booking = sync_rental(db, org, api, data["rental_id"])
        if booking:
            for msg in sync_messages(db, org, api, booking):
                handle_incoming(db, org, api, msg)
    return "ok"


def block_calendar(org: Organization, booking: Booking, release: bool = False) -> str | None:
    """Pose (ou retire) une indisponibilité Getaround pour une location directe, afin d'éviter la double réservation.

    Retourne un message d'erreur lisible, ou None si tout va bien / rien à faire.
    """
    api = client_for(org)
    if not api or not settings.get(org, "getaround_block_calendar") or not booking.vehicle.getaround_car_id:
        return None
    try:
        if release:
            api.delete_unavailability(booking.vehicle.getaround_car_id, booking.starts_at, booking.ends_at)
        else:
            api.create_unavailability(booking.vehicle.getaround_car_id, booking.starts_at, booking.ends_at)
    except GetaroundError as exc:
        return str(exc)
    return None

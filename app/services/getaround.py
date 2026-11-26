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
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from ..models import Booking, Customer, EventLog, Message, Organization, Vehicle
from ..timeutil import now, to_iso, to_local
from . import settings

log = logging.getLogger("sharedfleet.getaround")
BASE_URL = "https://api-eu.getaround.com/owner/v1"
RATE_LIMIT = 90  # appels par minute, sous les 100 autorisés
_calls: deque = deque()  # horodatage des derniers appels (partagé par tout le processus)
_rate_lock = threading.Lock()


class GetaroundError(Exception):
    pass


class GetaroundClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=BASE_URL, timeout=30, transport=transport, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"})

    def _throttle(self) -> None:
        """Reste sous la limite Getaround (100 appels / minute / IP) : attend si 90 appels ont eu lieu en 60 s."""
        with _rate_lock:
            t = time.monotonic()
            while _calls and _calls[0] < t - 60:
                _calls.popleft()
            if len(_calls) >= RATE_LIMIT:
                time.sleep(max(0.0, _calls[0] + 60 - t))
            _calls.append(time.monotonic())

    def _request(self, method: str, url: str, **kw):
        try:
            self._throttle()
            r = self.http.request(method, url, **kw)
            if r.status_code == 429:  # limite atteinte malgré tout (autre appareil, même IP) : une seule nouvelle tentative
                time.sleep(min(60.0, float(r.headers.get("retry-after") or 30)))
                r = self.http.request(method, url, **kw)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            hint = {401: "jeton refusé : vérifiez qu'il est complet, sans le mot « Bearer », et toujours actif",
                    403: "accès refusé : ce jeton n'a pas les droits sur cette ressource",
                    404: "ressource introuvable",
                    429: "trop d'appels (limite Getaround : 100 par minute) — réessayez dans une minute"}.get(code, "erreur du serveur Getaround")
            raise GetaroundError(f"Getaround a répondu {code} ({hint}).") from exc
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

    def car(self, car_id) -> dict:
        return self._request("GET", f"/cars/{car_id}.json").json()

    def invoice_ids(self, start: datetime, end: datetime) -> list[int]:
        return [i["id"] for i in self._list("/invoices.json", {"start_date": to_iso(start), "end_date": to_iso(end)})]

    def invoice(self, invoice_id) -> dict:
        return self._request("GET", f"/invoices/{invoice_id}.json").json()

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


def link_car(db: Session, org: Organization, api: GetaroundClient, car_id, tried: set | None = None) -> Vehicle | None:
    """Rattache une voiture par sa fiche (/cars/{id}) quand elle n'apparaît pas dans la liste /cars.

    Certains comptes reçoivent une liste de voitures vide alors que les locations sont accessibles : la voiture est
    alors retrouvée à partir de l'identifiant indiqué dans la location. `tried` évite de la redemander à chaque location.
    """
    if tried is not None:
        if car_id in tried:
            return None
        tried.add(car_id)
    try:
        car = api.car(car_id)
    except GetaroundError as exc:
        log.warning("Fiche de la voiture Getaround %s illisible : %s", car_id, exc)
        return None
    plate = normalize_plate(car.get("plate_number", ""))
    vehicle = next((v for v in db.query(Vehicle).filter_by(org_id=org.id) if plate and normalize_plate(v.plate) == plate), None)
    if vehicle:
        vehicle.getaround_car_id = car["id"]
        db.commit()
    else:
        log.warning("Voiture Getaround %s (%s) : aucun véhicule SharedFleet avec cette plaque", car_id, car.get("plate_number"))
    return vehicle


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


def sync_rental(db: Session, org: Organization, api: GetaroundClient, rental_id, canceled: bool = False,
                relink: bool = True, tried_cars: set | None = None) -> Booking | None:
    booking = db.query(Booking).filter_by(org_id=org.id, source="getaround", external_id=str(rental_id)).first()
    if canceled:
        if booking:
            booking.status = "cancelled"
            db.commit()
        return booking
    r = api.rental(rental_id)
    find = lambda: db.query(Vehicle).filter_by(org_id=org.id, getaround_car_id=r["car_id"]).first()  # noqa: E731
    vehicle = find()
    if not vehicle and relink:  # voiture pas encore rattachée (premier webhook avant toute synchronisation) : on rattache puis on réessaie
        sync_cars(db, org, api)
        vehicle = find()
    if not vehicle:
        vehicle = link_car(db, org, api, r["car_id"], tried_cars)
    if not vehicle:
        log.warning("Location Getaround %s ignorée : voiture %s non rattachée à un véhicule", rental_id, r["car_id"])
        return None
    if not booking:
        booking = Booking(org_id=org.id, source="getaround", external_id=str(rental_id), vat_rate=0)
        db.add(booking)
    booking.vehicle_id = vehicle.id
    booking.starts_at, booking.ends_at = to_local(r["starts_at"]), to_local(r["ends_at"])
    if booking.deductions is None:  # un revenu net importé (avec ses retenues) est plus juste que le prix de l'API
        cents = (r.get("price") or 0) + ((r.get("insurance_fee") or 0) if settings.get(org, "getaround_include_insurance") else 0)
        booking.price_ht = cents / 100  # l'API exprime les montants en centimes
    known = booking.customer and booking.customer.getaround_user_id == r.get("user_id")
    if not known:  # conducteur déjà connu : inutile de le recharger à chaque synchronisation
        try:
            booking.customer = _upsert_driver(db, org, api, r["user_id"])
        except GetaroundError as exc:
            log.warning("Conducteur %s illisible : %s", r.get("user_id"), exc)
    if booking.status != "cancelled":
        booking.status = status_for(booking.starts_at, booking.ends_at)
    db.commit()
    return booking


def status_for(starts_at: datetime, ends_at: datetime) -> str:
    """Statut d'une location non annulée d'après ses dates."""
    t = now()
    return "completed" if ends_at < t else "in_progress" if starts_at <= t else "confirmed"


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


# /rentals.json filtre sur la DATE DE RÉSERVATION (« Find rentals booked between dates ») : on remonte assez loin
# pour couvrir les locations réservées longtemps à l'avance ; une fenêtre dans le futur ne renverrait rien.
BOOKED_DAYS_BACK = 90


def sync_all(db: Session, org: Organization, days_back: int = BOOKED_DAYS_BACK, days_ahead: int = 0) -> dict:
    """Synchronisation complète : voitures, locations de la fenêtre, messages des locations récentes."""
    api = client_for(org)
    if not api:
        raise GetaroundError("Jeton Getaround non configuré.")
    from .assistant import handle_incoming  # import tardif : évite la dépendance circulaire

    stats = {"cars": sync_cars(db, org, api), "rentals": 0, "messages": 0, "ignored": 0, "skipped": 0}
    t = now()
    tried_cars: set = set()  # voitures dont la fiche a déjà été demandée pendant cette synchronisation
    # Locations déjà complètes (conducteur connu) et terminées depuis plus de 7 jours : elles ne bougent plus
    settled = {b.external_id for b in db.query(Booking).filter(
        Booking.org_id == org.id, Booking.source == "getaround", Booking.customer_id.is_not(None),
        Booking.ends_at < t - timedelta(days=7))}
    for rid in api.rental_ids(t - timedelta(days=days_back), t + timedelta(days=days_ahead)):
        if str(rid) in settled:
            stats["skipped"] += 1
            continue
        if sync_rental(db, org, api, rid, relink=False, tried_cars=tried_cars):  # liste des voitures déjà lue au-dessus
            stats["rentals"] += 1
        else:
            stats["ignored"] += 1
    # Messages : seulement les locations à venir, en cours, ou terminées depuis moins de 7 jours
    recent = db.query(Booking).filter(Booking.org_id == org.id, Booking.source == "getaround",
                                      Booking.status != "cancelled", Booking.ends_at > t - timedelta(days=7))
    for booking in recent:
        try:
            fresh = sync_messages(db, org, api, booking)
        except GetaroundError as exc:
            # Location inconnue de l'API (par exemple importée à la main) : on passe à la suivante
            log.warning("Messages de la location %s illisibles : %s", booking.reference, exc)
            continue
        for msg in fresh:
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


def calendar_skip_reason(org: Organization, booking: Booking) -> str | None:
    """Pourquoi le créneau ne sera PAS bloqué sur Getaround (None = il le sera)."""
    if booking.historical:
        return "reprise d'historique"
    if not settings.get(org, "getaround_block_calendar"):
        return "option désactivée dans les réglages"
    if not settings.has(org, "getaround_token"):
        return "Getaround n'est pas connecté (aucun jeton d'API)"
    if not booking.vehicle.getaround_car_id:
        return f"{booking.vehicle.name} n'est pas relié à sa voiture Getaround"
    return None


def block_calendar(org: Organization, booking: Booking, release: bool = False) -> str | None:
    """Pose (ou retire) une indisponibilité Getaround pour une location directe, afin d'éviter la double réservation.

    Retourne un message d'erreur lisible, ou None si tout va bien / rien à faire.
    """
    api = client_for(org)
    if not api or calendar_skip_reason(org, booking):
        return None
    try:
        if release:
            api.delete_unavailability(booking.vehicle.getaround_car_id, booking.starts_at, booking.ends_at)
        else:
            api.create_unavailability(booking.vehicle.getaround_car_id, booking.starts_at, booking.ends_at)
    except GetaroundError as exc:
        return str(exc)
    return None


def diagnose(db: Session, org: Organization, days_back: int = BOOKED_DAYS_BACK, days_ahead: int = 0) -> list[dict]:
    """Vérifie la connexion étape par étape. Chaque étape : {ok, title, detail, lines}. S'arrête à la première
    étape bloquante. Limité à une vingtaine d'appels pour rester loin de la limite de 100 par minute."""
    steps: list[dict] = []

    def step(ok, title, detail="", items=None):
        steps.append({"ok": ok, "title": title, "detail": detail, "lines": items or []})
        return ok

    token = settings.get(org, "getaround_token")
    if not step(bool(token), "Jeton d'API enregistré",
                f"{len(token)} caractères" if token else "Aucun jeton : collez-le dans Réglages → Getaround puis Enregistrer."):
        return steps
    if token.lower().startswith("bearer "):
        step(False, "Format du jeton", "Le jeton commence par « Bearer » : collez uniquement la suite.")
        return steps
    api = GetaroundClient(token)
    try:
        cars = api.cars()
    except GetaroundError as exc:
        step(False, "Connexion à l'API Getaround", str(exc))
        return steps
    step(True, "Connexion à l'API Getaround", "Jeton accepté.")

    vehicles = db.query(Vehicle).filter_by(org_id=org.id).all()
    by_plate = {normalize_plate(v.plate): v for v in vehicles}
    items, linked = [], 0
    for car in cars:
        v = by_plate.get(normalize_plate(car.get("plate_number", "")))
        linked += bool(v)
        name = " ".join(x for x in (car.get("brand"), car.get("model")) if x)
        items.append(f"{car.get('plate_number') or '?'} — {name or 'voiture'} ({car.get('state', '?')}) → "
                     + (f"relié à « {v.name} »" if v else "aucun véhicule SharedFleet avec cette plaque"))
    if not cars:
        step(False, "Liste des voitures du compte", "Getaround renvoie une liste de voitures vide pour ce jeton. "
             "Le test continue avec les locations et les factures pour savoir si le jeton donne accès à vos données.")
    elif not vehicles:
        step(False, "Voitures Getaround ↔ véhicules SharedFleet",
             "Aucun véhicule créé dans SharedFleet : ajoutez vos véhicules avec la même plaque que sur Getaround.", items)
    else:
        step(linked == len(cars), "Voitures Getaround ↔ véhicules SharedFleet",
             f"Les {len(cars)} voiture(s) sont reliées à vos véhicules." if linked == len(cars) else
             f"{linked} voiture(s) sur {len(cars)} reliée(s). Les locations des autres sont ignorées : "
             "corrigez la plaque de vos véhicules dans SharedFleet pour qu'elle corresponde.", items)

    t = now()
    start, end = t - timedelta(days=days_back), t + timedelta(days=days_ahead)
    try:
        ids = api.rental_ids(start, end)
    except GetaroundError as exc:
        step(False, "Locations", str(exc))
        return steps
    rental_lines, rental_cars_ok = [], 0
    for rid in ids[:3]:  # quelques locations suffisent pour vérifier le rattachement des voitures
        try:
            r = api.rental(rid)
            car = api.car(r["car_id"])
            v = by_plate.get(normalize_plate(car.get("plate_number", "")))
            rental_cars_ok += 1
            rental_lines.append(f"Location {rid} du {to_local(r['starts_at']):%d/%m/%Y} → voiture {car.get('plate_number') or r['car_id']} → "
                                + (f"relié à « {v.name} »" if v else "aucun véhicule SharedFleet avec cette plaque"))
        except (GetaroundError, KeyError) as exc:
            rental_lines.append(f"Location {rid} : lecture impossible ({exc})")
    step(bool(ids), "Locations accessibles",
         f"{len(ids)} location(s) réservée(s) entre le {start:%d/%m/%Y} et le {end:%d/%m/%Y}." if ids else
         f"Aucune location réservée entre le {start:%d/%m/%Y} et le {end:%d/%m/%Y} (Getaround filtre sur la date de "
         "réservation, pas sur la date de location).", rental_lines)

    try:
        invoices = api.invoice_ids(t - timedelta(days=30), t)
        entity = api.invoice(invoices[0]).get("entity_type", "") if invoices else ""
        step(True, "Factures accessibles (30 derniers jours)",
             f"{len(invoices)} facture(s)" + (f", émises pour un compte « {'Société' if entity == 'Company' else 'Particulier'} »." if entity else ".")
             if invoices else "Aucune facture sur les 30 derniers jours.")
    except (GetaroundError, IndexError) as exc:
        step(False, "Factures", str(exc))
        invoices = []

    # Conclusion
    if not cars and not ids and not invoices:
        step(False, "Conclusion", "Le jeton est valide mais ne donne accès à aucune donnée (ni voiture, ni location, ni "
             "facture). Il est très probablement rattaché à un autre compte ou à une autre entité (particulier / société) "
             "que celui qui détient vos voitures. Demandez à Getaround de vérifier le compte associé au jeton.")
    elif not cars and ids and rental_cars_ok:
        step(True, "Conclusion", "La liste des voitures est vide mais les locations et les fiches des voitures sont "
             "accessibles : SharedFleet retrouve désormais les voitures à partir des locations. Lancez la synchronisation.")
    elif not cars and ids:
        step(False, "Conclusion", "Les locations sont visibles mais pas les fiches des voitures : le jeton n'a pas tous les "
             "droits. Demandez à Getaround un accès complet à l'API propriétaire pour ce compte.")
    return steps

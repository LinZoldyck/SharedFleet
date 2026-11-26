"""Import JSON des locations directes déjà réalisées (« reprise d'historique »).

Chaque ligne devient une location directe en reprise d'historique : jamais de code d'accès, de SMS / e-mail,
de blocage du calendrier Getaround ni de demande d'état des lieux. Clé d'idempotence : le n° de contrat d'origine.
"""
from sqlalchemy.orm import Session

from ..models import Booking, Customer, Organization, Vehicle
from . import bookings as svc
from .getaround import normalize_plate, status_for
from .getaround_import import ACTIONS, ImportDataError, Row, _date, _number, _plain, parse, summary  # noqa: F401

EXAMPLE = """[
  {
    "contrat_ref": "SAH-2026-001",
    "plaque": "DS-301-FV",
    "client": "Lumen Événementiel",
    "debut": "2026-11-03T08:00",
    "fin": "2026-11-04T18:00",
    "prix_ht": 180.00,
    "tva": 20,
    "km_inclus": 400,
    "depot": 500
  }
]"""


def _customers(db: Session, org: Organization) -> dict[str, list[Customer]]:
    """Clients directs indexés par raison sociale, « prénom nom », nom et prénom (sans accents ni majuscules)."""
    index: dict[str, list[Customer]] = {}
    for c in db.query(Customer).filter_by(org_id=org.id, getaround_user_id=None):
        for key in {c.company_name, c.person, f"{c.last_name} {c.first_name}"}:
            if key.strip():
                index.setdefault(_plain(key), []).append(c)
    return index


def plan(db: Session, org: Organization, text: str) -> list[Row]:
    vehicles = {normalize_plate(v.plate): v for v in db.query(Vehicle).filter_by(org_id=org.id)}
    customers = _customers(db, org)
    rows, seen = [], set()
    for i, item in enumerate(parse(text), start=1):
        row = Row(index=i)
        rows.append(row)
        if not isinstance(item, dict):
            row.reasons.append("La ligne doit être un objet { … }.")
            continue
        row.getaround_id = str(item.get("contrat_ref") or "").strip()[:60]  # clé de la ligne (affichée en 1re colonne)
        row.plate = str(item.get("plaque") or "").strip()
        client = str(item.get("client") or "").strip()
        if not row.getaround_id:
            row.reasons.append("« contrat_ref » est obligatoire (n° du contrat d'origine).")
        elif row.getaround_id in seen:
            row.reasons.append(f"Contrat {row.getaround_id} présent plusieurs fois dans le tableau.")
        if not row.plate:
            row.reasons.append("« plaque » est obligatoire.")
        else:
            row.vehicle = vehicles.get(normalize_plate(row.plate))
            if not row.vehicle:
                row.reasons.append(f"Plaque {row.plate} inconnue : aucun véhicule SharedFleet avec cette immatriculation.")
        matches = customers.get(_plain(client), []) if client else []
        if not client:
            row.reasons.append("« client » est obligatoire (raison sociale, ou prénom et nom).")
        elif not matches:
            row.reasons.append(f"Client « {client} » introuvable : créez-le d'abord dans Clients (l'import n'en crée pas).")
        elif len({c.id for c in matches}) > 1:
            row.reasons.append(f"Plusieurs clients s'appellent « {client} » : précisez (raison sociale ou prénom et nom).")
        customer = matches[0] if len({c.id for c in matches}) == 1 else None
        row.renter = customer.label if customer else client
        row.starts_at, row.ends_at = _date(item.get("debut"), "debut", row), _date(item.get("fin"), "fin", row)
        row.price = _number(item.get("prix_ht"), "prix_ht", row)
        vat = _number(item.get("tva"), "tva", row)
        km = _number(item.get("km_inclus"), "km_inclus", row, integer=True)
        deposit = _number(item.get("depot"), "depot", row)
        for key, value in (("debut", row.starts_at), ("fin", row.ends_at), ("prix_ht", row.price)):
            if value is None and not any(f"« {key} »" in r for r in row.reasons):
                row.reasons.append(f"« {key} » est obligatoire.")
        if row.starts_at and row.ends_at and row.ends_at <= row.starts_at:
            row.reasons.append("« fin » doit être postérieure à « debut ».")
        if row.reasons:
            continue
        seen.add(row.getaround_id)
        days = max(1, int(-(-(row.ends_at - row.starts_at).total_seconds() // 86400)))
        row.values = {"vehicle_id": row.vehicle.id, "customer_id": customer.id, "starts_at": row.starts_at,
                      "ends_at": row.ends_at, "price_ht": row.price, "vat_rate": 20.0 if vat is None else vat,
                      "km_included": km if km is not None else row.vehicle.km_included * days,
                      "deposit": deposit if deposit is not None else row.vehicle.deposit}
        row.booking = db.query(Booking).filter_by(org_id=org.id, source="direct", contract_ref=row.getaround_id).first()
        _decide(db, org, row)
    return rows


LABELS = {"vehicle_id": "véhicule", "customer_id": "client", "starts_at": "début", "ends_at": "fin", "price_ht": "prix HT",
          "vat_rate": "TVA", "km_included": "km inclus", "deposit": "dépôt"}


def _decide(db: Session, org: Organization, row: Row) -> None:
    b = row.booking
    clash = [c for c in svc.conflicts(db, org.id, row.vehicle.id, row.starts_at, row.ends_at) if not b or c.id != b.id]
    warning = [f"Attention : chevauche {clash[0].reference} sur ce véhicule."] if clash else []
    if not b:
        row.action, row.reasons = "create", ["Nouvelle location en reprise d'historique."] + warning
        return
    changes = [LABELS[k] for k, v in row.values.items() if getattr(b, k) != v]
    if not b.historical:
        changes.append("passage en reprise d'historique")
    row.action, row.reasons = ("update", ["Modifié : " + ", ".join(changes)] + warning) if changes else ("unchanged", ["Déjà à jour."])


def apply(db: Session, org: Organization, rows: list[Row]) -> None:
    """Applique les lignes valides en une transaction. N'envoie jamais rien au client."""
    for row in rows:
        if row.action not in ("create", "update"):
            continue
        b = row.booking
        if not b:
            b = row.booking = Booking(org_id=org.id, source="direct")
            db.add(b)
        for key, value in row.values.items():
            setattr(b, key, value)
        svc.mark_historical(b, row.getaround_id)
        if b.status != "cancelled":
            b.status = status_for(b.starts_at, b.ends_at)
    db.commit()

"""Import manuel des locations Getaround (en attendant que la synchro par l'API fonctionne).

Un agent recopie les locations de l'espace propriétaire Getaround dans un tableau JSON. Chaque ligne devient une
location `source = "getaround"`, exactement comme celles de la synchro API (même clé : `external_id` = id Getaround),
pour que la future synchro les reconnaisse au lieu de créer des doublons.

Ce n'est PAS le circuit des locations directes : aucun client exigé, aucun contrat, aucun code d'accès (SMS / e-mail),
aucun blocage du calendrier Getaround, aucune facture.
"""
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Booking, Organization, Vehicle
from ..timeutil import now, to_local
from . import settings
from .getaround import normalize_plate, status_for

RENTER_PREFIX, KM_PREFIX = "Locataire : ", "Km parcourus : "
MAX_ROWS = 500
ACTIONS = {"create": ("Créer", "green"), "update": ("Mettre à jour", "blue"), "unchanged": ("Inchangée", "gray"),
           "cancel": ("Annuler", "amber"), "error": ("Erreur", "red")}


class ImportDataError(ValueError):
    """Erreur qui empêche de lire le JSON dans son ensemble."""


@dataclass
class Row:
    index: int  # numéro de ligne dans le tableau (à partir de 1)
    getaround_id: str = ""
    plate: str = ""
    vehicle: Vehicle | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    price: float | None = None
    km: int | None = None
    renter: str = ""
    cancelled: bool = False
    action: str = "error"
    reasons: list[str] = field(default_factory=list)
    booking: Booking | None = None

    @property
    def label(self):
        return ACTIONS[self.action]


def _plain(text: str) -> str:
    return unicodedata.normalize("NFD", str(text)).encode("ascii", "ignore").decode().strip().lower()


def _number(value, name: str, row: Row, integer: bool = False):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        row.reasons.append(f"« {name} » doit être un nombre.")
        return None
    try:
        number = float(str(value).replace(",", ".").replace(" ", "")) if isinstance(value, str) else float(value)
    except ValueError:
        row.reasons.append(f"« {name} » doit être un nombre (reçu : {value!r}).")
        return None
    if number < 0:
        row.reasons.append(f"« {name} » ne peut pas être négatif.")
        return None
    if integer and number != int(number):
        row.reasons.append(f"« {name} » doit être un nombre entier.")
        return None
    return int(number) if integer else round(number, 2)


def _date(value, name: str, row: Row) -> datetime | None:
    if not value:
        return None
    try:
        return to_local(str(value).strip())  # sans décalage : heure de Paris ; avec décalage : converti
    except ValueError:
        row.reasons.append(f"« {name} » n'est pas une date valide (format attendu : 2026-11-27T12:00, reçu : {value!r}).")
        return None


def parse(text: str) -> list:
    if not text.strip():
        raise ImportDataError("Collez un tableau JSON de locations.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportDataError(f"JSON invalide, ligne {exc.lineno} colonne {exc.colno} : {exc.msg}. "
                           "(Les commentaires // ne sont pas autorisés en JSON.)") from None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ImportDataError("Le JSON doit être un tableau [ … ] de locations.")
    if not data:
        raise ImportDataError("Le tableau est vide.")
    if len(data) > MAX_ROWS:
        raise ImportDataError(f"Trop de lignes ({len(data)}) : {MAX_ROWS} au maximum par import.")
    return data


def _notes(existing: str, renter: str, km: int | None) -> str:
    """Remplace les lignes gérées par l'import (locataire, km) en conservant toute autre note."""
    kept = [line for line in (existing or "").splitlines() if not line.startswith((RENTER_PREFIX, KM_PREFIX))]
    if renter:
        kept.append(RENTER_PREFIX + renter)
    if km is not None:
        kept.append(f"{KM_PREFIX}{km}")
    return "\n".join(kept)


def plan(db: Session, org: Organization, text: str) -> list[Row]:
    """Analyse le JSON et calcule, sans rien écrire, l'action prévue pour chaque ligne."""
    vehicles = {normalize_plate(v.plate): v for v in db.query(Vehicle).filter_by(org_id=org.id)}
    with_insurance = bool(settings.get(org, "getaround_include_insurance"))
    rows, seen = [], set()
    for i, item in enumerate(parse(text), start=1):
        row = Row(index=i)
        rows.append(row)
        if not isinstance(item, dict):
            row.reasons.append("La ligne doit être un objet { … }.")
            continue
        row.getaround_id = str(item.get("getaround_id") or "").strip()
        row.plate = str(item.get("plaque") or "").strip()
        statut = _plain(item.get("statut") or "")
        if not row.getaround_id:
            row.reasons.append("« getaround_id » est obligatoire.")
        elif row.getaround_id in seen:
            row.reasons.append(f"getaround_id {row.getaround_id} présent plusieurs fois dans le tableau : seule la première ligne est prise en compte.")
        if not row.plate:
            row.reasons.append("« plaque » est obligatoire.")
        else:
            row.vehicle = vehicles.get(normalize_plate(row.plate))
            if not row.vehicle:
                row.reasons.append(f"Plaque {row.plate} inconnue : aucun véhicule SharedFleet avec cette immatriculation.")
        if statut not in ("confirmee", "annulee"):
            row.reasons.append(f"« statut » doit valoir \"confirmee\" ou \"annulee\" (reçu : {item.get('statut')!r}).")
        row.cancelled = statut == "annulee"
        row.starts_at, row.ends_at = _date(item.get("debut"), "debut", row), _date(item.get("fin"), "fin", row)
        amount = _number(item.get("montant"), "montant", row)
        insurance = _number(item.get("assurance"), "assurance", row)
        row.km = _number(item.get("km"), "km", row, integer=True)
        row.renter = str(item.get("locataire") or "").strip()[:200]
        if not row.cancelled:
            for key, value in (("debut", row.starts_at), ("fin", row.ends_at), ("montant", amount)):
                if value is None and not any(f"« {key} »" in r for r in row.reasons):
                    row.reasons.append(f"« {key} » est obligatoire pour une location confirmée.")
        if row.starts_at and row.ends_at and row.ends_at <= row.starts_at:
            row.reasons.append("« fin » doit être postérieure à « debut ».")
        if amount is not None:
            # Même règle que la synchro API : l'assurance ne compte dans le revenu que si l'option est cochée
            row.price = round(amount + ((insurance or 0) if with_insurance else 0), 2)
        if row.reasons:
            continue  # ligne en erreur : rien ne sera créé ni modifié
        seen.add(row.getaround_id)
        row.booking = db.query(Booking).filter_by(org_id=org.id, source="getaround", external_id=row.getaround_id).first()
        _decide(row)
    return rows


def _decide(row: Row) -> None:
    b = row.booking
    if row.cancelled:
        if not b:
            row.action, row.reasons = "unchanged", ["Location annulée inconnue de SharedFleet : rien à créer."]
        elif b.status == "cancelled":
            row.action, row.reasons = "unchanged", ["Déjà annulée."]
        else:
            row.action, row.reasons = "cancel", ["Marquée annulée (conservée dans l'historique)."]
        return
    if not b:
        row.action, row.reasons = "create", [f"Nouvelle location sur {row.vehicle.name}."]
        return
    changes = []
    if b.vehicle_id != row.vehicle.id:
        changes.append(f"véhicule : {b.vehicle.name} → {row.vehicle.name}")
    if (b.starts_at, b.ends_at) != (row.starts_at, row.ends_at):
        changes.append(f"dates : {b.starts_at:%d/%m %Hh%M}–{b.ends_at:%d/%m %Hh%M} → {row.starts_at:%d/%m %Hh%M}–{row.ends_at:%d/%m %Hh%M}")
    if round(b.price_ht, 2) != row.price:
        changes.append(f"montant : {b.price_ht:.2f} € → {row.price:.2f} €")
    if b.notes != _notes(b.notes, row.renter or b.imported_renter, row.km if row.km is not None else b.imported_km):
        changes.append("locataire / km")
    if b.status == "cancelled":
        changes.append("réactivée (annulée auparavant)")
    elif b.status != status_for(row.starts_at, row.ends_at):
        changes.append("statut selon les dates")
    row.action, row.reasons = ("update", changes) if changes else ("unchanged", ["Déjà à jour."])


def apply(db: Session, org: Organization, rows: list[Row]) -> None:
    """Applique les lignes valides en une seule transaction. Les lignes en erreur sont ignorées."""
    for row in rows:
        if row.action in ("error", "unchanged"):
            continue
        b = row.booking
        if row.action == "cancel":
            b.status = "cancelled"
            continue
        if row.action == "create":
            b = row.booking = Booking(org_id=org.id, source="getaround", external_id=row.getaround_id, vat_rate=0,
                                      created_at=now())
            db.add(b)
        b.vehicle_id = row.vehicle.id
        b.starts_at, b.ends_at, b.price_ht = row.starts_at, row.ends_at, row.price
        b.notes = _notes(b.notes, row.renter or b.imported_renter, row.km if row.km is not None else b.imported_km)
        b.status = status_for(row.starts_at, row.ends_at)  # une ligne « confirmee » réactive une location annulée
    db.commit()


def summary(rows: list[Row]) -> dict[str, int]:
    counts = {key: 0 for key in ACTIONS}
    for row in rows:
        counts[row.action] += 1
    return counts


EXAMPLE = """[
  {
    "getaround_id": "123456",
    "plaque": "ER-836-AN",
    "debut": "2026-11-27T12:00",
    "fin": "2026-11-28T22:00",
    "montant": 85.40,
    "km": 120,
    "locataire": "Wesilé T.",
    "statut": "confirmee"
  }
]"""

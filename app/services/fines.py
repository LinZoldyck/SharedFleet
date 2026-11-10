"""Contraventions : lecture des avis déposés dans un dossier, puis rapprochement avec la location concernée.

Deux familles : « amende » (avis de contravention ANTAI : excès de vitesse, stationnement gênant… → le
conducteur peut être désigné) et « fps » (forfait post-stationnement → payé par le titulaire puis refacturé).
"""
import base64
import hashlib
import logging
import re
import shutil
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from sqlalchemy.orm import Session

from .. import config
from ..models import Booking, Fine, Organization, Vehicle
from . import settings
from .getaround import normalize_plate

log = logging.getLogger("sharedfleet.fines")
MEDIA_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class FineData(BaseModel):
    kind: Literal["amende", "fps"] = Field(description="fps = forfait post-stationnement ; amende = tout autre avis de contravention")
    notice_number: str = Field(description="Numéro de l'avis de contravention ou de l'avis de paiement FPS, sans espaces. Vide si illisible.")
    plate: str = Field(description="Immatriculation du véhicule, format AA-123-AA. Vide si illisible.")
    offense_date: str = Field(description="Date de l'infraction ou du stationnement, format AAAA-MM-JJ. Vide si absente.")
    offense_time: str = Field(description="Heure de l'infraction, format HH:MM sur 24 h. Vide si absente.")
    offense_type: str = Field(description="Nature de l'infraction en quelques mots")
    location: str = Field(description="Lieu de l'infraction")
    amount: Optional[float] = Field(description="Montant à payer en euros (montant forfaitaire, non minoré ni majoré)")


PROMPT = ("Voici un avis de contravention ou un avis de paiement de forfait post-stationnement (FPS) français "
          "reçu par un loueur de véhicules. Extrais les informations demandées. La date et l'heure sont celles "
          "de l'infraction (ou du constat de stationnement), pas celles d'édition de l'avis. Laisse un champ vide "
          "plutôt que de deviner.")


def extract_with_claude(org: Organization, path: Path) -> FineData | None:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    media_type = MEDIA_TYPES[path.suffix.lower()]
    source = {"type": "base64", "media_type": media_type, "data": data}
    block = {"type": "document", "source": source} if media_type == "application/pdf" else {"type": "image", "source": source}
    client = anthropic.Anthropic(api_key=settings.get(org, "anthropic_api_key"))
    try:
        response = client.beta.messages.parse(
            model=config.CLAUDE_MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}],
            output_format=FineData, output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    except anthropic.RateLimitError:
        log.warning("Claude : limite de débit atteinte pendant la lecture de %s", path.name)
        return None
    except anthropic.APIStatusError as exc:
        log.error("Claude a répondu %s pendant la lecture de %s : %s", exc.status_code, path.name, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        log.error("Claude injoignable : %s", exc)
        return None
    return None if response.stop_reason == "refusal" else response.parsed_output


PLATE_RE = re.compile(r"\b([A-HJ-NP-TV-Z]{2})[\s-]?(\d{3})[\s-]?([A-HJ-NP-TV-Z]{2})\b")
DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(20\d{2})\b")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])\s?(?:h|:)\s?([0-5]\d)\b", re.I)
NOTICE_RE = re.compile(r"\b(\d{10,26})\b")
AMOUNT_RE = re.compile(r"(\d{1,4}(?:[.,]\d{2})?)\s?(?:€|euros?)", re.I)


def extract_with_regex(path: Path) -> FineData | None:
    """Lecture de secours sans IA : uniquement pour les PDF contenant du texte (pas les scans)."""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    except (PyPdfError, OSError, ValueError):
        return None
    if not text.strip():
        return None
    plate, date_, time_ = PLATE_RE.search(text), DATE_RE.search(text), TIME_RE.search(text)
    notice, amount = NOTICE_RE.search(text.replace(" ", "")), AMOUNT_RE.search(text)
    is_fps = bool(re.search(r"post[- ]stationnement|\bFPS\b", text, re.I))
    return FineData(
        kind="fps" if is_fps else "amende",
        notice_number=notice.group(1) if notice else "",
        plate="-".join(plate.groups()) if plate else "",
        offense_date=f"{date_.group(3)}-{date_.group(2)}-{date_.group(1)}" if date_ else "",
        offense_time=f"{int(time_.group(1)):02d}:{time_.group(2)}" if time_ else "",
        offense_type="", location="",
        amount=float(amount.group(1).replace(",", ".")) if amount else None)


def _offense_datetime(d: FineData) -> tuple[datetime | None, bool]:
    """(date-heure, heure_connue)"""
    try:
        day = datetime.strptime(d.offense_date, "%Y-%m-%d")
    except ValueError:
        return None, False
    try:
        t = datetime.strptime(d.offense_time, "%H:%M").time()
        return datetime.combine(day, t), True
    except ValueError:
        return day, False


def match(db: Session, fine: Fine, time_known: bool = True) -> None:
    """Rapproche l'avis d'un véhicule (plaque) puis de la location couvrant la date de l'infraction."""
    fine.vehicle_id = fine.booking_id = None
    plate = normalize_plate(fine.plate)
    vehicle = next((v for v in db.query(Vehicle).filter_by(org_id=fine.org_id) if normalize_plate(v.plate) == plate), None) if plate else None
    if not vehicle or not fine.offense_at:
        fine.status = "to_review"
        return
    fine.vehicle_id = vehicle.id
    q = db.query(Booking).filter(Booking.org_id == fine.org_id, Booking.vehicle_id == vehicle.id, Booking.status != "cancelled")
    if time_known:
        found = q.filter(Booking.starts_at <= fine.offense_at, Booking.ends_at >= fine.offense_at).all()
    else:  # heure inconnue : toute location qui touche ce jour-là
        day_end = datetime.combine(fine.offense_at.date(), time.max)
        found = q.filter(Booking.starts_at <= day_end, Booking.ends_at >= fine.offense_at).all()
    if len(found) == 1:
        fine.booking_id, fine.status = found[0].id, "matched"
    elif found:
        fine.status = "to_review"  # plusieurs locations possibles : un humain tranche
    else:
        fine.status = "unmatched"  # véhicule non loué à ce moment-là


def process_file(db: Session, org: Organization, path: Path) -> Fine | None:
    """Lit un avis, crée la contravention et la rapproche. Retourne None si le fichier est un doublon."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if db.query(Fine).filter_by(org_id=org.id, file_hash=digest).first():
        return None
    data, method = None, ""
    if settings.has(org, "anthropic_api_key"):
        data, method = extract_with_claude(org, path), "claude"
    if data is None:
        data, method = extract_with_regex(path), "regex"
    stored = config.org_dir(org.id, "contraventions", "traitees") / f"{digest[:12]}{path.suffix.lower()}"
    shutil.copyfile(path, stored)
    fine = Fine(org_id=org.id, file_path=str(stored.relative_to(config.DATA_DIR)), file_hash=digest)
    time_known = False
    if data:
        fine.offense_at, time_known = _offense_datetime(data)
        fine.kind, fine.notice_number, fine.plate = data.kind, re.sub(r"\s", "", data.notice_number), data.plate.upper()
        fine.offense_type, fine.location, fine.amount, fine.extraction = data.offense_type[:200], data.location[:300], data.amount, method
    db.add(fine)
    match(db, fine, time_known)
    db.commit()
    return fine


def inbox(org_id: int) -> Path:
    return config.org_dir(org_id, "contraventions", "a_traiter")


def scan_inbox(db: Session, org: Organization) -> int:
    """Traite les fichiers déposés dans <DATA_DIR>/org_<id>/contraventions/a_traiter puis les retire du dossier."""
    count = 0
    for path in sorted(inbox(org.id).iterdir()):
        if not path.is_file() or path.suffix.lower() not in MEDIA_TYPES or path.name.startswith("."):
            continue
        try:
            if process_file(db, org, path):
                count += 1
            path.unlink()
        except Exception:  # noqa: BLE001 — un fichier illisible ne doit pas bloquer les suivants
            log.exception("Lecture impossible de %s", path.name)
            db.rollback()
            path.rename(path.with_name("." + path.name + ".erreur"))
    return count


def designation(org: Organization, fine: Fine) -> dict:
    """Données à saisir pour désigner le conducteur (utilisées par l'extension Chrome)."""
    b = fine.booking
    c = b.customer if b else None
    data = {
        "id": fine.id, "kind": fine.kind, "notice_number": fine.notice_number, "plate": fine.plate,
        "offense_at": fine.offense_at.strftime("%d/%m/%Y %H:%M") if fine.offense_at else "",
        "offense_date": fine.offense_at.strftime("%d/%m/%Y") if fine.offense_at else "",
        "offense_type": fine.offense_type, "amount": fine.amount,
        "booking": b.reference if b else "", "source": b.source if b else "",
    }
    if c:
        data["driver"] = {
            "is_company": bool(c.company_name), "company_name": c.company_name, "siret": c.siret,
            "first_name": c.first_name, "last_name": c.last_name,
            "birth_date": c.birth_date.strftime("%d/%m/%Y") if c.birth_date else "",
            "address": c.address, "postal_code": c.postal_code, "city": c.city, "country": c.country or "France",
            "email": c.email, "phone": c.phone, "license_number": c.license_number,
            "license_date": c.license_date.strftime("%d/%m/%Y") if c.license_date else "",
            "license_country": c.license_country or "France",
        }
    return data

"""Prise de vue guidée de l'état des lieux (une case par photo, illustrée)."""
import io
from datetime import timedelta

from pypdf import PdfReader
from sqlalchemy import text

from app.db import add_missing_columns, engine
from app.models import Booking, Photo
from app.services import photo_guide
from app.timeutil import now

from .conftest import new_client, photo

REQUIRED = [s.key for s in photo_guide.REQUIRED]


def start_rental(client, fleet, db):
    vehicle, customer = fleet
    start = now() + timedelta(minutes=10)
    client.post("/locations", data={"vehicle_id": vehicle.id, "customer_id": customer.id,
                                    "starts_at": start.isoformat(timespec="minutes"),
                                    "ends_at": (start + timedelta(hours=8)).isoformat(timespec="minutes")})
    b = db.query(Booking).one()
    guest = new_client()
    guest.post("/acces", data={"code": b.access_code})
    return b, guest


FORM = {"mileage": "45000", "fuel_level": "80", "signed_name": "Inès Moreau", "accept": "on"}


def test_page_shows_one_illustrated_slot_per_shot(client, fleet, db):
    b, guest = start_rental(client, fleet, db)
    page = guest.get("/acces/etat-des-lieux/checkin").text
    for slot in photo_guide.SLOTS:
        assert f'name="{slot.key}"' in page and slot.label in page
    assert page.count("<svg") >= len(photo_guide.SLOTS) and page.count('capture="environment"') == len(photo_guide.SLOTS)
    assert page.count("data-required") == len(REQUIRED) == 5
    assert "3/4 avant gauche" in page and "Rétroviseur droit" in page and "Tableau de bord" in page


def test_guided_photos_are_saved_in_order_with_their_label(client, fleet, db):
    b, guest = start_rental(client, fleet, db)
    files = [(key, photo()) for key in REQUIRED + ["ext_retro_g"]] + [("exterior", photo("#aa3333"))]  # + 1 dégât
    r = guest.post("/acces/etat-des-lieux/checkin", data=FORM, files=files)
    assert r.headers["location"] == "/acces/location"
    labels = [(p.category, p.label) for p in db.query(Photo).order_by(Photo.id)]
    assert labels == [("exterior", "3/4 avant gauche"), ("exterior", "3/4 avant droit"), ("exterior", "3/4 arrière gauche"),
                      ("exterior", "3/4 arrière droit"), ("exterior", "Rétroviseur gauche"), ("exterior", ""),
                      ("interior", "Tableau de bord")]
    home = guest.get("/acces/location").text
    assert "<span>3/4 avant gauche</span>" in home and "<span>Autre photo</span>" in home
    pdf = client.get(f"/locations/{b.id}/contrat.pdf").content
    text_ = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "3/4 arrière droit" in text_ and "Tableau de bord" in text_ and "Rétroviseur gauche" in text_
    assert "3/4 avant gauche" in client.get(f"/locations/{b.id}").text  # légende côté loueur


def test_missing_required_shot_is_refused_and_nothing_is_saved(client, fleet, db):
    b, guest = start_rental(client, fleet, db)
    files = [(key, photo()) for key in REQUIRED if key != "int_dash"] + [("ext_face", photo())]
    r = guest.post("/acces/etat-des-lieux/checkin", data=FORM, files=files)
    assert r.headers["location"].endswith("/checkin")
    assert "Il manque des photos obligatoires : Tableau de bord." in guest.get("/acces/etat-des-lieux/checkin").text
    assert db.query(Photo).count() == 0 and db.query(Booking).one().status == "confirmed"


def test_old_photo_form_is_still_accepted(client, fleet, db):
    """Le formulaire du loueur (et une page restée ouverte pendant la mise à jour) envoie encore « exterior / interior »."""
    b, guest = start_rental(client, fleet, db)
    r = guest.post("/acces/etat-des-lieux/checkin", data=FORM, files=[("exterior", photo())] * 4 + [("interior", photo())])
    assert r.headers["location"] == "/acces/location" and db.query(Photo).count() == 5


def test_photo_label_column_is_added_to_existing_databases(db):
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE photos DROP COLUMN label"))
    assert add_missing_columns() == ["photos.label"]


def test_illustrations_are_valid_svg():
    import xml.etree.ElementTree as ET
    for slot in photo_guide.SLOTS:
        root = ET.fromstring(str(photo_guide.illustration(slot)))
        assert root.tag.endswith("svg") and root.get("aria-label") == slot.label

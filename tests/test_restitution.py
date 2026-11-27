"""Contrat avec photos à chaque étape, et décompte de fin de location (km supplémentaires, carburant)."""
import io
import re
from datetime import timedelta

import pytest
from pypdf import PdfReader
from sqlalchemy import text

from app.db import add_missing_columns, engine
from app.models import Booking, Invoice, Vehicle
from app.services import bookings as svc
from app.services import invoicing
from app.timeutil import now

from .conftest import new_client, photo


def pdf_photos(content: bytes) -> int:
    return sum(len(page.images) for page in PdfReader(io.BytesIO(content)).pages)


@pytest.fixture
def rental(client, fleet, db):
    """Location directe qui démarre dans 10 minutes ; véhicule : 200 km/jour inclus, 0,30 € HT/km, réservoir 80 L à 1,85 €."""
    vehicle, customer = fleet
    vehicle.fuel_tank_liters, vehicle.fuel_price = 80, 1.85
    db.commit()
    start = now() + timedelta(minutes=10)
    client.post("/locations", data={"vehicle_id": vehicle.id, "customer_id": customer.id,
                                    "starts_at": start.isoformat(timespec="minutes"),
                                    "ends_at": (start + timedelta(hours=8)).isoformat(timespec="minutes")})
    b = db.query(Booking).one()
    guest = new_client()
    guest.post("/acces", data={"code": b.access_code})
    return b, guest


FILES = [("exterior", photo("#778899"))] * 4 + [("interior", photo("#aa8866"))]


def checkin(guest, km=45000, fuel=80):
    return guest.post("/acces/etat-des-lieux/checkin", files=FILES,
                      data={"mileage": str(km), "fuel_level": str(fuel), "signed_name": "Inès Moreau", "accept": "on"})


def checkout(guest, km, fuel):
    return guest.post("/acces/etat-des-lieux/checkout", files=FILES, data={"mileage": str(km), "fuel_level": str(fuel)})


def contract_link(guest):
    return re.search(r'href="(/acces/contrat\.pdf\?v=[^"]+)"', guest.get("/acces/location").text).group(1)


# --- bug : photos absentes du contrat téléchargé par le locataire ---
def test_renter_contract_contains_photos_at_each_stage_and_is_never_cached(rental, db):
    b, guest = rental
    first = contract_link(guest)
    r = guest.get(first)
    assert pdf_photos(r.content) == 0 and 'filename="contrat-' + b.reference + '-initial.pdf"' in r.headers["content-disposition"]
    assert "no-store" in r.headers["cache-control"]  # le téléphone ne doit jamais resservir une ancienne version

    checkin(guest)
    after_start = contract_link(guest)
    assert after_start != first  # nouvelle adresse : impossible de rouvrir le PDF sans photos
    r = guest.get(after_start)
    assert pdf_photos(r.content) == 5 and "-depart.pdf" in r.headers["content-disposition"]
    assert "avec les photos de départ" in guest.get("/acces/location").text

    checkout(guest, km=45100, fuel=80)
    final = contract_link(guest)
    r = guest.get(final)
    assert pdf_photos(r.content) == 10 and "-final.pdf" in r.headers["content-disposition"]
    assert "Télécharger mon contrat final (avec photos)" in guest.get("/acces/location").text


def test_photos_are_shown_on_the_renter_screen(rental, db):
    b, guest = rental
    checkin(guest)
    page = guest.get("/acces/location").text
    assert "Vos photos de départ" in page
    ids = re.findall(r'src="/acces/photo/(\d+)"', page)
    assert len(ids) == 5 and guest.get(f"/acces/photo/{ids[0]}").status_code == 200
    assert new_client().get(f"/acces/photo/{ids[0]}").status_code == 303  # privé : il faut le code de la location


def test_staff_contract_has_photos_and_no_cache(client, rental, db):
    b, guest = rental
    checkin(guest)
    r = client.get(f"/locations/{b.id}/contrat.pdf")
    assert pdf_photos(r.content) == 5 and "no-store" in r.headers["cache-control"]


# --- décompte de fin de location ---
def test_extra_km_and_missing_fuel_are_due(rental, db):
    b, guest = rental
    checkin(guest, km=45000, fuel=80)
    checkout(guest, km=45300, fuel=60)  # 300 km pour 200 inclus ; 20 % de 80 L = 16 L manquants
    db.refresh(b)
    s = svc.settlement(b)
    assert s["km"] == {"driven": 300, "included": 200, "extra": 100, "unit_ht": 0.30, "ht": 30.0, "ttc": 36.0}
    assert (s["fuel"]["liters"], s["fuel"]["ttc"], s["fuel"]["ht"]) == (16.0, 29.6, 24.67)
    assert (s["total_ttc"], s["total_ht"]) == (65.6, 54.67)
    page = guest.get("/acces/location").text
    assert "100 km supplémentaires" in page and "16,0 L manquants" in page and "À payer" in page and "65,60 €" in page
    lines = invoicing.booking_lines(b)
    assert [round(l["quantity"] * l["unit_price"], 2) for l in lines[1:]] == [30.0, 24.67]
    assert "Carburant manquant" in lines[2]["label"] and "16,0 L" in lines[2]["label"]


def test_extra_fuel_is_refunded(client, rental, db):
    b, guest = rental
    checkin(guest, km=45000, fuel=50)
    checkout(guest, km=45150, fuel=75)  # pas de km en plus ; 25 % de 80 L = 20 L rendus en plus
    db.refresh(b)
    s = svc.settlement(b)
    assert s["km"]["extra"] == 0 and s["fuel"]["ttc"] == -37.0 and s["total_ttc"] == -37.0
    page = guest.get("/acces/location").text
    assert "rendus en plus" in page and "À vous rembourser" in page and "37,00 €" in page
    line = invoicing.booking_lines(b)[-1]
    assert "remboursement" in line["label"] and line["unit_price"] < 0
    client.post(f"/locations/{b.id}/facturer")
    inv = db.query(Invoice).one()
    assert inv.amount_ttc == round(b.price_ttc - 37.0, 2)  # la facture déduit le remboursement
    assert "Décompte de fin de location" in client.get(f"/locations/{b.id}").text


def test_same_fuel_level_and_unpriced_vehicle(rental, db):
    b, guest = rental
    checkin(guest, fuel=70)
    checkout(guest, km=45050, fuel=70)
    db.refresh(b)
    assert svc.settlement(b)["fuel"]["ttc"] == 0 and "Rien à régler" in guest.get("/acces/location").text
    # véhicule sans réservoir ni prix renseigné : seul l'écart est indiqué, sans montant inventé
    b.vehicle.fuel_tank_liters = 0
    b.inspection("checkout").fuel_level = 40
    db.commit()
    s = svc.settlement(b)
    assert s["fuel"]["priced"] is False and s["fuel"]["ttc"] == 0
    page = guest.get("/acces/location").text
    assert "le montant vous sera communiqué" in page
    assert len(invoicing.booking_lines(b)) == 1  # aucune ligne carburant sans prix


def test_contract_pdf_contains_the_settlement(client, rental, db):
    b, guest = rental
    checkin(guest, km=45000, fuel=80)
    checkout(guest, km=45300, fuel=60)
    text_ = "".join(page.extract_text() for page in PdfReader(io.BytesIO(client.get(f"/locations/{b.id}/contrat.pdf").content)).pages)
    assert "Décompte de fin de location" in text_ and "100 km supplémentaires" in text_ and "16,0 L manquants" in text_
    assert "À payer par le locataire" in text_ and "65,60 €" in text_
    assert "départ 5 photo(s) · retour 5 photo(s)" in text_


def test_vehicle_fuel_settings_and_migration(client, fleet, db):
    vehicle, _ = fleet
    client.post(f"/vehicules/{vehicle.id}", data={"name": "Master", "plate": "AB-123-CD", "fuel_tank_liters": "80",
                                                  "fuel_price": "1,85", "active": "on"})
    db.refresh(vehicle)
    assert (vehicle.fuel_tank_liters, vehicle.fuel_price) == (80, 1.85)
    with engine.begin() as conn:
        for col in ("fuel_tank_liters", "fuel_price"):
            conn.execute(text(f"ALTER TABLE vehicles DROP COLUMN {col}"))
    assert sorted(add_missing_columns()) == ["vehicles.fuel_price", "vehicles.fuel_tank_liters"]
    db.expire_all()
    v = db.get(Vehicle, vehicle.id)
    assert (v.fuel_tank_liters, v.fuel_price, v.name) == (0, 0, "Master")

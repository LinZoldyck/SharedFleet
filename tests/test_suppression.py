"""Suppression définitive des locations directes."""
import json
from datetime import timedelta

import pytest

from app import config
from app.models import Booking, EventLog, Fine, Inspection, Invoice, Organization, Photo
from app.services import bookings as svc
from app.services import getaround, settings
from app.timeutil import now

from .conftest import new_client, photo


def book(client, vehicle, customer, days=3, **extra):
    start = now() + timedelta(days=days)
    return client.post("/locations", data={"vehicle_id": vehicle.id, "customer_id": customer.id,
                                           "starts_at": start.isoformat(timespec="minutes"),
                                           "ends_at": (start + timedelta(hours=8)).isoformat(timespec="minutes"), **extra})


@pytest.fixture
def no_send(monkeypatch):
    sent = []
    monkeypatch.setattr(svc, "send_sms", lambda *a: sent.append("sms") or True)
    monkeypatch.setattr("app.services.email.send", lambda *a: sent.append("email"))
    return sent


def test_delete_removes_rental_inspections_and_photo_files(client, fleet, db, no_send):
    vehicle, customer = fleet
    book(client, vehicle, customer)
    b = db.query(Booking).one()
    client.post(f"/locations/{b.id}/etat-des-lieux/checkin", data={"mileage": "100", "fuel_level": "80"},
                files=[("exterior", photo()), ("interior", photo())])
    files = [config.DATA_DIR / ph.path for ph in db.query(Photo)]
    assert len(files) == 2 and all(f.exists() for f in files)
    db.add(EventLog(org_id=b.org_id, kind="access_code", key=f"booking:{b.id}", detail="sms|ok|06"))
    db.commit()
    bid, ref = b.id, b.reference
    page = client.get(f"/locations/{bid}").text
    assert f"Supprimer définitivement {ref}" in page
    r = client.post(f"/locations/{bid}/supprimer")
    assert r.headers["location"] == "/locations"
    assert f"Location {ref} supprimée définitivement" in client.get("/locations").text
    db.expire_all()
    assert db.query(Booking).count() == 0 and db.query(Inspection).count() == 0 and db.query(Photo).count() == 0
    assert not any(f.exists() for f in files)  # le dossier lui-même est retiré s'il est vide
    assert db.query(EventLog).filter_by(kind="access_code").count() == 0
    trace = db.query(EventLog).filter_by(kind="booking_deleted").one()  # trace de qui a supprimé quoi
    assert ref in trace.detail and "Alice Admin" in trace.detail and "Lumen Events" in trace.detail
    assert client.get(f"/locations/{bid}").status_code == 404 and no_send == []


def test_reference_is_never_reused(client, fleet, db):
    vehicle, customer = fleet
    book(client, vehicle, customer, days=3)
    book(client, vehicle, customer, days=5)
    last = db.query(Booking).order_by(Booking.id.desc()).first()
    deleted_ref = last.reference
    client.post(f"/locations/{last.id}/supprimer")
    book(client, vehicle, customer, days=7)
    db.expire_all()
    refs = [b.reference for b in db.query(Booking).order_by(Booking.id)]
    assert deleted_ref not in refs and len(refs) == 2


def test_batch_import_still_gets_distinct_ids(client, fleet, db):
    rows = [{"getaround_id": str(i), "plaque": "AB123CD", "debut": f"2026-12-0{i}T09:00", "fin": f"2026-12-0{i}T18:00",
             "montant": 50, "statut": "confirmee"} for i in range(1, 4)]
    client.post("/locations/import-getaround", data={"payload": json.dumps(rows), "action": "import"})
    assert len({b.id for b in db.query(Booking)}) == 3


def test_future_rental_frees_its_getaround_slot(client, fleet, db, monkeypatch):
    vehicle, customer = fleet
    calls = []
    settings.update(db.query(Organization).one(), {"getaround_token": "TOKEN"})
    vehicle.getaround_car_id = 9
    db.commit()
    monkeypatch.setattr(getaround, "block_calendar", lambda org, b, release=False: calls.append(release) or None)
    book(client, vehicle, customer)
    b = db.query(Booking).one()
    client.post(f"/locations/{b.id}/supprimer")
    assert calls == [False, True]  # bloqué à la création, libéré à la suppression
    calls.clear()
    book(client, vehicle, customer, days=-3, historical="on")  # reprise d'historique : rien sur Getaround
    client.post(f"/locations/{db.query(Booking).one().id}/supprimer")
    assert calls == [] and db.query(Booking).count() == 0


def test_getaround_slot_release_failure_is_reported(client, fleet, db, monkeypatch):
    vehicle, customer = fleet
    book(client, vehicle, customer)
    monkeypatch.setattr(getaround, "block_calendar", lambda org, b, release=False: "Getaround a répondu 500")
    client.post(f"/locations/{db.query(Booking).one().id}/supprimer")
    page = client.get("/locations").text
    assert "supprimée définitivement" in page and "libérez-le à la main" in page


def test_invoiced_or_fined_rentals_cannot_be_deleted(client, fleet, db):
    vehicle, customer = fleet
    book(client, vehicle, customer, days=-3, historical="on")
    b = db.query(Booking).one()
    client.post(f"/locations/{b.id}/facturer")
    assert "est facturée" in client.get(f"/locations/{b.id}").text
    client.post(f"/locations/{b.id}/supprimer")
    assert db.query(Booking).count() == 1 and "est facturée" in client.get(f"/locations/{b.id}").text
    db.query(Invoice).delete()
    db.add(Fine(org_id=b.org_id, file_path="", file_hash="h", booking_id=b.id, vehicle_id=vehicle.id, status="matched"))
    db.commit()
    client.post(f"/locations/{b.id}/supprimer")
    assert db.query(Booking).count() == 1 and "contravention(s) sont rattachées" in client.get(f"/locations/{b.id}").text


def test_only_admins_and_only_direct_rentals(client, fleet, db):
    vehicle, customer = fleet
    book(client, vehicle, customer)
    b = db.query(Booking).one()
    client.post("/utilisateurs", data={"name": "G", "email": "g@test.fr", "role": "manager", "password": "motdepasse-solide"})
    manager = new_client()
    manager.post("/connexion", data={"email": "g@test.fr", "password": "motdepasse-solide"})
    assert manager.post(f"/locations/{b.id}/supprimer").status_code == 403
    assert "Supprimer définitivement" not in manager.get(f"/locations/{b.id}").text
    other = new_client()
    other.post("/inscription", data={"company": "Autre", "name": "Bob", "email": "bob@autre.fr", "password": "motdepasse-solide"})
    assert other.post(f"/locations/{b.id}/supprimer").status_code == 404  # autre société
    assert db.query(Booking).count() == 1
    client.post("/locations/import-getaround", data={"payload": json.dumps([{
        "getaround_id": "9", "plaque": "AB123CD", "debut": "2026-12-20T09:00", "fin": "2026-12-20T18:00",
        "montant": 50, "statut": "confirmee"}]), "action": "import"})
    ga = db.query(Booking).filter_by(source="getaround").one()
    assert "Supprimer définitivement" not in client.get(f"/locations/{ga.id}").text
    client.post(f"/locations/{ga.id}/supprimer")
    assert db.query(Booking).filter_by(source="getaround").count() == 1

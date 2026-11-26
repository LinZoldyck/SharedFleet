"""Reprise d'historique et corrections associées (revenus négatifs, statut d'après les dates, dépôt, fiche client,
synchro Getaround). Contrainte transversale vérifiée partout : aucun SMS / e-mail ne part lors d'une reprise."""
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

from app.db import add_missing_columns, engine
from app.models import Booking, Customer, EventLog, Invoice, Organization, Vehicle
from app.services import bookings as svc
from app.services import dashboard, getaround, scheduler, settings
from app.timeutil import now

from .conftest import new_client


@pytest.fixture
def no_send(monkeypatch):
    """Échoue si quoi que ce soit tente d'envoyer un SMS ou un e-mail."""
    sent = []
    monkeypatch.setattr(svc, "send_sms", lambda *a: sent.append("sms") or True)
    monkeypatch.setattr("app.services.email.send", lambda *a: sent.append("email"))
    return sent


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def book(client, vehicle, customer, start, end, **extra):
    return client.post("/locations", data={"vehicle_id": vehicle.id, "customer_id": customer.id,
                                           "starts_at": iso(start), "ends_at": iso(end), **extra})


# --- 0. migration de la base existante ---
def test_existing_database_gets_new_columns_without_losing_data(client, fleet, db):
    vehicle, customer = fleet
    book(client, vehicle, customer, now() + timedelta(days=3), now() + timedelta(days=4))
    with engine.begin() as conn:  # base « d'avant » : colonnes absentes
        for col in ("historical", "contract_ref", "deductions"):
            conn.execute(text(f"ALTER TABLE bookings DROP COLUMN {col}"))
    assert sorted(add_missing_columns()) == ["bookings.contract_ref", "bookings.deductions", "bookings.historical"]
    db.expire_all()
    b = db.query(Booking).one()
    assert (b.historical, b.contract_ref, b.deductions, b.vehicle_id) == (False, "", None, vehicle.id)
    assert add_missing_columns() == []  # rien à refaire au démarrage suivant


# --- 1. revenus Getaround négatifs ---
def gimport(client, rows):
    return client.post("/locations/import-getaround", data={"payload": json.dumps(rows), "action": "import"}).text


def test_negative_net_revenue_with_deductions(client, fleet, db):
    vehicle, _ = fleet
    rows = [{"getaround_id": "10468713", "plaque": "AB-123-CD", "debut": "2026-09-12T09:00", "fin": "2026-09-12T19:00",
             "montant": -51.20, "retenues": 103.40, "statut": "confirmee"}]
    page = gimport(client, rows)
    assert "Créer : 1" in page and "Erreur : 0" in page
    b = db.query(Booking).one()
    assert (b.price_ht, b.deductions, b.gross_amount) == (-51.20, 103.40, 52.20)
    assert "Inchangée : 1" in gimport(client, rows)  # toujours idempotent
    assert 'class="r num neg"' in client.get("/locations?f=getaround").text  # en rouge dans la liste
    detail = client.get(f"/locations/{b.id}").text
    assert "52,20 € brut − 103,40 € de retenues" in detail
    d = dashboard.compute(db, b.org_id, "annee")
    assert next(x for x in d["per_vehicle"] if x["vehicle"].id == vehicle.id)["revenue"] == -51.20
    # l'ancien format (sans « retenues ») reste valable, montant nul accepté
    page = gimport(client, [{"getaround_id": "2", "plaque": "AB123CD", "debut": "2026-09-20T09:00",
                             "fin": "2026-09-20T12:00", "montant": 0, "statut": "confirmee"}])
    assert "Créer : 1" in page
    assert gimport(client, [{**rows[0], "retenues": -5}]).count("ne peut pas être négatif") == 1


# --- 2. statut d'après les dates ---
def test_status_is_derived_from_dates(client, fleet, db):
    vehicle, customer = fleet
    for days in (-10, -5):  # comme LOC-00071 / 72 : passées, sans état des lieux
        book(client, vehicle, customer, now() + timedelta(days=days), now() + timedelta(days=days, hours=8), historical="on")
    book(client, vehicle, customer, now() - timedelta(hours=20), now() + timedelta(hours=10), historical="on")  # LOC-00073
    book(client, vehicle, customer, now() + timedelta(days=4), now() + timedelta(days=5))
    past1, past2, running, future = db.query(Booking).order_by(Booking.starts_at).all()
    assert all(b.status == "confirmed" for b in (past1, past2, running, future))  # statut stocké inchangé
    assert [b.effective_status for b in (past1, past2, running, future)] == ["completed", "completed", "in_progress", "confirmed"]
    future.status = "cancelled"
    db.commit()
    assert future.effective_status == "cancelled"
    actives = client.get("/locations").text.split("<tbody>")[-1]
    assert running.reference in actives and past1.reference not in actives and future.reference not in actives
    assert "Terminée" in client.get(f"/locations/{past1.id}").text and "En cours" in client.get(f"/locations/{running.id}").text
    future.status = "confirmed"
    db.commit()
    future.status = "completed"  # retour anticipé constaté par état des lieux : jamais de recul
    assert future.effective_status == "completed"


# --- 3. reprise d'historique ---
def test_past_start_requires_the_history_box(client, fleet, db, no_send):
    vehicle, customer = fleet
    r = book(client, vehicle, customer, now() - timedelta(days=2), now() - timedelta(days=1))
    assert r.headers["location"] == "/locations/nouvelle" and db.query(Booking).count() == 0
    assert "Le départ est dans le passé" in client.get("/locations/nouvelle").text
    # un départ « à l'instant » reste possible (client devant le véhicule)
    assert book(client, vehicle, customer, now() - timedelta(minutes=5), now() + timedelta(hours=4)).headers["location"].startswith("/locations/")


def test_history_mode_sends_nothing_and_blocks_nothing(client, fleet, db, no_send, monkeypatch):
    vehicle, customer = fleet
    org = db.query(Organization).one()
    settings.update(org, {"getaround_token": "TOKEN", "email_provider": "console"})
    vehicle.getaround_car_id = 9
    db.commit()
    blocked = []
    monkeypatch.setattr(getaround, "block_calendar", lambda *a, **kw: blocked.append(a) or None)
    # client avec téléphone ET e-mail, départ dans 10 minutes : la fenêtre d'envoi normale
    r = book(client, vehicle, customer, now() + timedelta(minutes=10), now() + timedelta(hours=8),
             historical="on", contract_ref="SAH-2026-004", deposit="500")
    b = db.query(Booking).one()
    assert (b.historical, b.contract_ref, b.access_code, b.deposit) == (True, "SAH-2026-004", None, 500.0)
    assert scheduler.send_due_codes() == 0 and no_send == [] and blocked == []
    assert svc.send_access_code(db, org, b) == {}
    client.post(f"/locations/{b.id}/code", data={"channel": "sms"})
    assert no_send == [] and db.query(EventLog).filter_by(kind="access_code").count() == 0
    page = client.get(f"/locations/{b.id}").text
    assert "Signé hors application" in page and "SAH-2026-004" in page and "En attente d&#39;acceptation" not in page
    assert "à faire" not in page and "non concerné" in page and "aucun SMS ni e-mail" in page
    assert db.query(Invoice).count() == 0 and "Créer la facture" in page  # pas de facture auto, bouton conservé
    pdf = client.get(f"/locations/{b.id}/contrat.pdf")
    assert pdf.status_code == 200
    assert svc.find_by_code(db, "") is None and b.access_code is None  # page client inaccessible : aucun code


def test_admin_converts_existing_rentals_without_sending(client, fleet, db, no_send):
    """LOC-00071/72/73 : déjà créées en mode normal, dont une en cours (73) qui recevrait son code."""
    vehicle, customer = fleet
    book(client, vehicle, customer, now() + timedelta(minutes=5), now() + timedelta(hours=12))
    b = db.query(Booking).one()
    assert b.access_code and svc.due_for_code(db) == [b]  # sans conversion, le code partirait à la prochaine minute
    from .conftest import new_client as _nc
    client.post("/utilisateurs", data={"name": "G", "email": "g@test.fr", "role": "manager", "password": "motdepasse-solide"})
    manager = _nc()
    manager.post("/connexion", data={"email": "g@test.fr", "password": "motdepasse-solide"})
    assert manager.post(f"/locations/{b.id}/historique", data={"contract_ref": "X"}).status_code == 403  # action admin
    client.post(f"/locations/{b.id}/historique", data={"contract_ref": "SAH-2026-004"})
    db.refresh(b)
    assert (b.historical, b.contract_ref, b.access_code) == (True, "SAH-2026-004", None)
    assert svc.due_for_code(db) == [] and scheduler.send_due_codes() == 0 and no_send == []


def test_direct_history_import(client, fleet, db, no_send):
    vehicle, customer = fleet
    db.add(Customer(org_id=customer.org_id, first_name="Carole"))
    db.commit()
    rows = [
        {"contrat_ref": "SAH-2026-001", "plaque": "AB123CD", "client": "Lumen Events", "debut": "2026-11-03T08:00",
         "fin": "2026-11-04T18:00", "prix_ht": 180, "tva": 20, "km_inclus": 400, "depot": 1000},
        {"contrat_ref": "SAH-2026-002", "plaque": "ab-123-cd", "client": "carole", "debut": "2026-11-10T08:00",
         "fin": "2026-11-10T18:00", "prix_ht": 89, "depot": 500},
        {"contrat_ref": "SAH-2026-003", "plaque": "ZZ999ZZ", "client": "Personne Inconnue", "debut": "2026-11-12T08:00",
         "fin": "2026-11-12T18:00", "prix_ht": 50},
    ]
    post = lambda rows, action="import": client.post("/locations/import-directes", data={"payload": json.dumps(rows), "action": action}).text  # noqa: E731
    page = post(rows, "preview")
    assert "Créer : 2" in page and "Erreur : 1" in page and "Plaque ZZ999ZZ inconnue" in page and "introuvable" in page
    assert db.query(Booking).count() == 0
    post(rows)
    bookings = {b.contract_ref: b for b in db.query(Booking)}
    assert set(bookings) == {"SAH-2026-001", "SAH-2026-002"}
    b2 = bookings["SAH-2026-002"]
    assert (b2.historical, b2.access_code, b2.deposit, b2.vat_rate, b2.customer.first_name) == (True, None, 500, 20, "Carole")
    assert b2.km_included == vehicle.km_included and b2.effective_status == "completed"
    assert "Inchangée : 2" in post(rows)  # idempotent
    rows[1]["depot"] = 300
    assert "Modifié : dépôt" in post(rows)
    db.expire_all()
    assert db.query(Booking).filter_by(contract_ref="SAH-2026-002").one().deposit == 300
    assert db.query(Booking).count() == 2 and no_send == [] and scheduler.send_due_codes() == 0


def test_import_matches_a_converted_rental_by_contract_ref(client, fleet, db, no_send):
    vehicle, customer = fleet
    book(client, vehicle, customer, now() - timedelta(days=2), now() - timedelta(days=1), historical="on",
         contract_ref="SAH-2026-001")
    row = {"contrat_ref": "SAH-2026-001", "plaque": "AB123CD", "client": "Lumen Events",
           "debut": iso(now() - timedelta(days=2)), "fin": iso(now() - timedelta(days=1)), "prix_ht": 90}
    page = client.post("/locations/import-directes", data={"payload": json.dumps([row]), "action": "import"}).text
    assert "Créer : 0" in page and db.query(Booking).count() == 1


# --- 4. dépôt de garantie ---
def test_deposit_per_vehicle_and_per_rental(client, fleet, db):
    vehicle, customer = fleet
    vehicle.deposit = 800
    db.commit()
    book(client, vehicle, customer, now() + timedelta(days=2), now() + timedelta(days=3))
    book(client, vehicle, customer, now() + timedelta(days=5), now() + timedelta(days=6), deposit="500")
    first, second = db.query(Booking).order_by(Booking.starts_at).all()
    assert (first.deposit, second.deposit) == (800, 500)
    client.post(f"/locations/{first.id}/modifier", data={"deposit": "650"})
    db.refresh(first)
    assert first.deposit == 650
    assert b"650" in client.get(f"/locations/{first.id}").content


# --- 5. fiche client ---
def test_first_name_only_customer_and_sending_warning(client, db):
    page = client.get("/clients/nouveau").text
    assert "déclenche l'<b>envoi automatique du code d'accès</b>" in page
    client.post("/clients/0", data={"first_name": "Carole"})
    c = db.query(Customer).filter_by(first_name="Carole").one()
    assert c.last_name == "" and c.label == "Carole"
    client.post("/clients/0", data={})
    assert "Indiquez une raison sociale, un nom ou un prénom" in client.get("/clients").text


# --- réponse à la question : blocage du calendrier Getaround ---
def test_calendar_block_status_is_always_explicit(client, fleet, db, monkeypatch):
    vehicle, customer = fleet
    book(client, vehicle, customer, now() + timedelta(days=2), now() + timedelta(days=3))
    assert "Créneau NON bloqué sur Getaround : Getaround n&#39;est pas connecté" in client.get("/locations").text
    org = db.query(Organization).one()
    settings.update(org, {"getaround_token": "TOKEN"})
    db.commit()
    book(client, vehicle, customer, now() + timedelta(days=5), now() + timedelta(days=6))
    assert "Master n&#39;est pas relié à sa voiture Getaround" in client.get("/locations").text
    vehicle.getaround_car_id = 9
    db.commit()
    monkeypatch.setattr(getaround, "block_calendar", lambda *a, **kw: None)
    book(client, vehicle, customer, now() + timedelta(days=8), now() + timedelta(days=9))
    assert "Créneau bloqué sur le calendrier Getaround." in client.get("/locations").text


# --- 6. synchro API ---
def test_sync_looks_back_on_booking_dates_and_spares_api_calls(client, fleet, db, monkeypatch):
    from .test_integrations import FakeGetaround
    fake = FakeGetaround(now() - timedelta(hours=2), now() + timedelta(hours=20))
    requests = []

    def handler(r):
        requests.append(r)
        return fake(r)

    monkeypatch.setattr(getaround, "client_for", lambda o: getaround.GetaroundClient("TOKEN", transport=httpx.MockTransport(handler)))
    org = db.query(Organization).one()
    getaround.sync_all(db, org)
    windows = [(r.url.params["start_date"], r.url.params["end_date"]) for r in requests if r.url.path.endswith("/rentals.json")]
    assert len(windows) == 3  # 90 jours de réservations, par tranches de 30 jours
    assert windows[-1][1][:10] == now().strftime("%Y-%m-%d")  # rien dans le futur : on filtre sur la date de réservation
    # la location est terminée depuis longtemps et son conducteur est connu : plus de rappel de l'API
    b = db.query(Booking).one()
    b.starts_at, b.ends_at = now() - timedelta(days=20), now() - timedelta(days=19)
    db.commit()
    requests.clear()
    stats = getaround.sync_all(db, org)
    assert stats["skipped"] == 1 and not any(r.url.path.endswith(("/rentals/77.json", "/users/501.json")) for r in requests)


def test_sync_keeps_imported_net_revenue_and_never_duplicates(client, fleet, db, monkeypatch):
    from .test_integrations import FakeGetaround
    gimport(client, [{"getaround_id": "77", "plaque": "AB123CD", "debut": "2026-09-12T09:00", "fin": "2026-09-12T19:00",
                      "montant": -51.20, "retenues": 103.40, "statut": "confirmee"}])
    fake = FakeGetaround(now() - timedelta(hours=2), now() + timedelta(hours=20))
    monkeypatch.setattr(getaround, "client_for", lambda o: getaround.GetaroundClient("TOKEN", transport=httpx.MockTransport(fake)))
    getaround.sync_all(db, db.query(Organization).one())
    b = db.query(Booking).one()
    assert b.reference == "GA-77" and b.price_ht == -51.20 and b.customer.last_name == "Martin"


def test_throttle_and_single_retry_on_429(monkeypatch):
    sleeps = []
    monkeypatch.setattr(getaround.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(getaround, "RATE_LIMIT", 3)
    answers = iter([httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200, json=[])] + [httpx.Response(200, json=[])] * 5)
    api = getaround.GetaroundClient("TOKEN", transport=httpx.MockTransport(lambda r: next(answers)))
    api._request("GET", "/cars.json")
    assert sleeps == [7.0]  # Retry-After respecté, puis succès
    for _ in range(3):
        api._request("GET", "/cars.json")
    assert len(sleeps) == 2  # 4e appel de la minute au-delà de la limite (3) : attente avant d'appeler

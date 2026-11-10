"""Parcours de bout en bout : comptes et rôles, location directe, parcours client, contrat, facture."""
from datetime import timedelta

from app.models import Booking, Invoice, User
from app.services import bookings as svc
from app.timeutil import now

from .conftest import new_client, photo


def book(client, vehicle, customer, start, end):
    return client.post("/locations", data={"vehicle_id": vehicle.id, "customer_id": customer.id,
                                           "starts_at": start.isoformat(), "ends_at": end.isoformat()})


def test_pages_require_login():
    c = new_client()
    for path in ("/", "/locations", "/reglages", "/contraventions", "/media/org_1/x.jpg"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/connexion"


def test_login_logout_and_bad_password(client):
    client.post("/deconnexion")
    assert client.get("/").status_code == 303
    assert client.post("/connexion", data={"email": "alice@test.fr", "password": "faux"}).headers["location"] == "/connexion"
    assert client.post("/connexion", data={"email": "alice@test.fr", "password": "motdepasse-solide"}).headers["location"] == "/"


def test_login_is_rate_limited(client):
    c = new_client()
    for _ in range(9):
        c.post("/connexion", data={"email": "x@test.fr", "password": "faux"})
    assert "Trop de tentatives" in c.get("/connexion").text


def test_cross_site_post_is_refused(client):
    r = client.post("/vehicules/0", data={"name": "X", "plate": "Y"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_roles(client, db):
    client.post("/utilisateurs", data={"name": "Vic", "email": "vic@test.fr", "role": "viewer", "password": "motdepasse-viewer"})
    viewer = new_client()
    viewer.post("/connexion", data={"email": "vic@test.fr", "password": "motdepasse-viewer"})
    assert viewer.get("/locations").status_code == 200
    assert viewer.get("/reglages").status_code == 403  # réglages : administrateurs seulement
    assert viewer.post("/vehicules/0", data={"name": "X", "plate": "Y"}).status_code == 403  # lecture seule
    # un administrateur ne peut pas se retirer ses propres droits
    me = db.query(User).filter_by(email="alice@test.fr").one()
    client.post(f"/utilisateurs/{me.id}", data={"role": "viewer", "active": "on"})
    db.refresh(me)
    assert me.role == "admin"


def test_tenants_are_isolated(client, fleet):
    vehicle, _ = fleet
    other = new_client()
    other.post("/inscription", data={"company": "Autre", "name": "Bob", "email": "bob@autre.fr", "password": "motdepasse-solide"})
    assert other.get(f"/vehicules/{vehicle.id}").status_code == 404
    assert "Master" not in other.get("/vehicules").text


def test_booking_conflict_is_refused(client, fleet, db):
    vehicle, customer = fleet
    start = now() + timedelta(days=2)
    assert book(client, vehicle, customer, start, start + timedelta(hours=8)).headers["location"].startswith("/locations/")
    r = book(client, vehicle, customer, start + timedelta(hours=2), start + timedelta(hours=10))
    assert r.headers["location"] == "/locations/nouvelle"
    assert db.query(Booking).count() == 1


def test_price_follows_daily_rate(client, fleet, db):
    vehicle, customer = fleet
    start = now() + timedelta(days=2)
    book(client, vehicle, customer, start, start + timedelta(hours=30))  # 30 h = 2 jours entamés
    b = db.query(Booking).one()
    assert (b.days, b.price_ht, b.km_included, b.price_ttc) == (2, 180.0, 400, 216.0)


def test_sms_code_goes_out_30_minutes_before(client, fleet, db, monkeypatch):
    vehicle, customer = fleet
    sent = []
    monkeypatch.setattr(svc, "send_sms", lambda org, to, text: sent.append((to, text)) or True)
    book(client, vehicle, customer, now() + timedelta(hours=3), now() + timedelta(hours=9))
    assert svc.due_for_code(db) == []  # trop tôt
    b = db.query(Booking).one()
    b.starts_at = now() + timedelta(minutes=20)
    db.commit()
    from app.services import scheduler
    assert scheduler.send_due_codes() == 1
    assert sent and b.access_code in sent[0][1] and "/acces" in sent[0][1]
    assert scheduler.send_due_codes() == 0  # pas de doublon


def test_customer_journey_contract_and_invoice(client, fleet, db):
    vehicle, customer = fleet
    book(client, vehicle, customer, now() + timedelta(minutes=10), now() + timedelta(hours=8))
    b = db.query(Booking).one()
    guest = new_client()

    assert guest.post("/acces", data={"code": "000000"}).headers["location"] == "/acces"  # mauvais code
    assert guest.get("/acces/location").headers["location"] == "/acces"  # pas d'accès sans code
    assert guest.post("/acces", data={"code": b.access_code}).headers["location"] == "/acces/location"
    home = guest.get("/acces/location").text
    assert "4821" in home and "état des lieux de départ" in home  # code du Master Lock visible

    form = {"mileage": "45000", "fuel_level": "80", "signed_name": "Inès Moreau", "accept": "on"}
    few = [("exterior", photo()), ("interior", photo())]
    assert guest.post("/acces/etat-des-lieux/checkin", data=form, files=few).headers["location"].endswith("/checkin")  # 4 photos exigées
    files = [("exterior", photo())] * 4 + [("interior", photo("#aa8866"))]
    assert guest.post("/acces/etat-des-lieux/checkin", data=form, files=files).headers["location"] == "/acces/location"
    db.refresh(b)
    assert b.status == "in_progress" and b.accepted_by == "Inès Moreau" and len(b.inspection("checkin").photos) == 5

    # retour : le kilométrage ne peut pas reculer ; 300 km parcourus pour 200 inclus
    back = {"mileage": "44000", "fuel_level": "50"}
    assert guest.post("/acces/etat-des-lieux/checkout", data=back, files=files).headers["location"].endswith("/checkout")
    back["mileage"] = "45300"
    guest.post("/acces/etat-des-lieux/checkout", data=back, files=files)
    db.refresh(b)
    assert b.status == "completed"
    assert "rendre les clés" in guest.get("/acces/location").text

    pdf = client.get(f"/locations/{b.id}/contrat.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and len(pdf.content) > 20000  # photos incluses
    assert guest.get("/acces/contrat.pdf").content.startswith(b"%PDF")
    photo_path = b.inspection("checkin").photos[0].path
    assert client.get(f"/media/{photo_path}").status_code == 200
    assert new_client().get(f"/media/{photo_path}").status_code == 303  # photos privées

    client.post(f"/locations/{b.id}/facturer")
    inv = db.query(Invoice).one()
    assert inv.number.startswith("F-") and inv.amount_ht == 90 + 100 * 0.30  # location + 100 km supplémentaires
    assert client.get(f"/factures/{inv.id}/pdf").content.startswith(b"%PDF")

    # une fois la session quittée, le code d'une location terminée ne donne plus accès
    guest.post("/acces/quitter")
    assert guest.post("/acces", data={"code": b.access_code}).headers["location"] == "/acces"


def test_access_code_window(client, fleet, db):
    vehicle, customer = fleet
    book(client, vehicle, customer, now() + timedelta(hours=5), now() + timedelta(hours=9))
    b = db.query(Booking).one()
    assert svc.find_by_code(db, b.access_code) is None  # plus de 30 min avant le départ
    b.starts_at = now() + timedelta(minutes=25)
    db.commit()
    assert svc.find_by_code(db, b.access_code).id == b.id
    b.status = "cancelled"
    db.commit()
    assert svc.find_by_code(db, b.access_code) is None


def test_dashboard_and_all_pages_render(client, fleet, db):
    vehicle, customer = fleet
    book(client, vehicle, customer, now() - timedelta(days=2), now() - timedelta(days=1))
    client.post("/couts", data={"spent_on": now().date().isoformat(), "category": "Carburant", "amount": "60", "vehicle_id": str(vehicle.id)})
    for path in ("/", "/?p=annee", "/locations", "/locations?f=toutes", "/vehicules", "/clients", "/messages", "/contraventions",
                 "/factures", "/couts", "/reglages", "/locations/nouvelle", "/locations/1", f"/vehicules/{vehicle.id}", f"/clients/{customer.id}"):
        assert client.get(path).status_code == 200, path
    from app.services import dashboard
    d = dashboard.compute(db, vehicle.org_id, "annee")
    assert d["revenue"] == 90 and d["variable_costs"] == 60 and d["bookings_count"] == 1
    assert 0 < d["occupancy"] <= 100 and d["margin"] < 90

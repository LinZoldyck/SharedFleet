"""Import manuel des locations Getaround (JSON collé dans /locations/import-getaround)."""
import json
from datetime import timedelta

import httpx
import pytest

from app.models import Booking, Customer, EventLog, Invoice, Organization, User, Vehicle
from app.services import dashboard, getaround, getaround_import, scheduler, settings
from app.timeutil import now

from .conftest import new_client

URL = "/locations/import-getaround"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def row(**kw):
    start = (now() + timedelta(days=2)).replace(hour=12, minute=0, second=0)
    base = {"getaround_id": "123456", "plaque": "AB123CD", "debut": iso(start), "fin": iso(start + timedelta(hours=34)),
            "montant": 85.40, "km": 120, "locataire": "Wesilé T.", "statut": "confirmee"}
    base.update(kw)
    return base


def post(client, rows, action="import"):
    payload = rows if isinstance(rows, str) else json.dumps(rows)
    return client.post(URL, data={"payload": payload, "action": action})


def test_creation_follows_the_getaround_circuit_not_the_direct_one(client, fleet, db, monkeypatch):
    vehicle, _ = fleet
    calls = []
    monkeypatch.setattr(getaround, "block_calendar", lambda *a, **kw: calls.append("block"))
    monkeypatch.setattr("app.services.bookings.send_sms", lambda *a: calls.append("sms") or True)
    customers_before = db.query(Customer).count()
    page = post(client, [row(plaque="ab 123-cd", debut=iso(now() + timedelta(minutes=10)),
                             fin=iso(now() + timedelta(hours=6)))]).text
    assert "Import effectué" in page and "Créer : 1" in page
    b = db.query(Booking).one()
    assert (b.source, b.external_id, b.reference, b.vehicle_id) == ("getaround", "123456", "GA-123456", vehicle.id)
    assert (b.price_ht, b.vat_rate, b.customer_id, b.access_code, b.deposit) == (85.40, 0, None, None, 0)
    assert b.renter_label == "Wesilé T." and b.imported_km == 120 and b.status == "confirmed"
    # rien du circuit des locations directes
    assert scheduler.send_due_codes() == 0  # pas de code d'accès, même 10 min avant le départ
    assert calls == [] and db.query(Customer).count() == customers_before
    assert db.query(Invoice).count() == 0 and db.query(EventLog).filter_by(kind="access_code").count() == 0
    detail = client.get(f"/locations/{b.id}").text
    assert "Contrat PDF" not in detail and "Créer la facture" not in detail and "Accès du client" not in detail
    assert "Wesilé T." in detail and "120 km" in detail
    # visible dans l'onglet Getaround, le calendrier et la rentabilité
    assert "GA-123456" in client.get("/locations?f=getaround").text
    assert "Wesilé T." in client.get("/locations").text  # bandeau de disponibilité
    d = dashboard.compute(db, b.org_id, "annee")
    assert d["revenue_by_source"]["getaround"] == 85.40
    assert next(x for x in d["per_vehicle"] if x["vehicle"].id == vehicle.id)["revenue"] == 85.40


def test_identical_reimport_changes_nothing(client, fleet, db):
    rows = [row(), row(getaround_id="777", debut=iso(now() - timedelta(days=3)), fin=iso(now() - timedelta(days=2)))]
    post(client, rows)
    snapshot = [(b.id, b.starts_at, b.ends_at, b.price_ht, b.notes, b.status) for b in db.query(Booking).order_by(Booking.id)]
    for _ in range(2):
        page = post(client, rows).text
        assert "Inchangée : 2" in page and "Créer : 0" in page and "Mettre à jour : 0" in page
    db.expire_all()
    assert [(b.id, b.starts_at, b.ends_at, b.price_ht, b.notes, b.status) for b in db.query(Booking).order_by(Booking.id)] == snapshot


def test_date_change_updates_the_same_booking(client, fleet, db):
    post(client, [row()])
    b = db.query(Booking).one()
    new_end = b.ends_at + timedelta(hours=5)
    page = post(client, [row(fin=iso(new_end), montant=99)], action="preview").text
    assert "Prévisualisation" in page and "Mettre à jour : 1" in page and "dates :" in page and "montant :" in page
    db.refresh(b)
    assert b.ends_at != new_end  # la prévisualisation n'écrit rien
    post(client, [row(fin=iso(new_end), montant=99)])
    db.expire_all()
    b = db.query(Booking).one()
    assert (b.ends_at, b.price_ht) == (new_end, 99.0)


def test_cancellation_marks_without_deleting(client, fleet, db):
    post(client, [row()])
    page = post(client, [{"getaround_id": "123456", "plaque": "AB-123-CD", "statut": "annulee"}]).text
    assert "Annuler : 1" in page
    b = db.query(Booking).one()
    db.refresh(b)
    assert b.status == "cancelled" and b.price_ht == 85.40  # conservée
    assert "Inchangée : 1" in post(client, [row(statut="annulee")]).text  # déjà annulée
    page = post(client, [row(getaround_id="999", statut="annulée")]).text  # accent accepté
    assert "Inchangée : 1" in page and "rien à créer" in page
    assert db.query(Booking).count() == 1  # une annulation inconnue ne crée rien
    page = post(client, [row()]).text  # reconfirmée par Getaround
    assert "réactivée" in page
    db.refresh(b)
    assert b.status == "confirmed"


def test_unknown_plate_is_an_error_and_creates_nothing(client, fleet, db):
    page = post(client, [row(getaround_id="1", plaque="ZZ-999-ZZ"), row(getaround_id="2")]).text
    assert "Erreur : 1" in page and "Créer : 1" in page and "Plaque ZZ-999-ZZ inconnue" in page
    assert [b.external_id for b in db.query(Booking)] == ["2"]


@pytest.mark.parametrize("payload,message", [
    ('[{"getaround_id": "1", "plaque": "AB123CD",}]', "JSON invalide, ligne 1"),
    ('[\n  {"getaround_id": "1" // commentaire\n}]', "commentaires // ne sont pas autorisés"),
    ('"bonjour"', "doit être un tableau"),
    ("[]", "tableau est vide"),
    ("", "Collez un tableau JSON"),
])
def test_invalid_json_is_rejected_entirely(client, fleet, db, payload, message):
    page = post(client, payload).text
    assert message in page and db.query(Booking).count() == 0
    if payload:  # le texte collé reste dans le champ pour pouvoir le corriger
        assert f'name="payload"' in page and payload.replace('"', "&#34;").replace("\n", "&#10;")[:12] in page.replace("\n", "&#10;")


def test_row_level_validation(client, fleet, db):
    rows = [row(getaround_id=""), row(getaround_id="5", debut="27/11/2026"), row(getaround_id="6", montant="beaucoup"),
            row(getaround_id="7", fin=row()["debut"]), row(getaround_id="8", statut="en cours"),
            row(getaround_id="9", montant=None), row(getaround_id="10", montant="85,40"), row(getaround_id="10")]
    page = post(client, rows, action="preview").text
    for reason in ("« getaround_id » est obligatoire", "« debut » n&#39;est pas une date valide", "« montant » doit être un nombre",
                   "« fin » doit être postérieure", "« statut » doit valoir", "« montant » est obligatoire",
                   "présent plusieurs fois"):
        assert reason in page, reason
    assert "Créer : 1" in page and "Erreur : 7" in page  # seule la ligne 10 (montant « 85,40 ») est valide


def test_insurance_option_is_respected(client, fleet, db):
    org = db.query(Organization).one()
    post(client, [row(assurance=8.00)])
    assert db.query(Booking).one().price_ht == 85.40  # option décochée : assurance ignorée
    settings.update(org, {"getaround_include_insurance": True})
    db.commit()
    page = post(client, [row(assurance=8.00)]).text
    assert "85.40 € → 93.40 €" in page
    db.expire_all()
    assert db.query(Booking).one().price_ht == 93.40


def test_access_is_limited_to_admin_and_manager(client, fleet, db):
    for role, email_ in (("manager", "gil@test.fr"), ("viewer", "vic@test.fr")):
        client.post("/utilisateurs", data={"name": role, "email": email_, "role": role, "password": "motdepasse-solide"})
    manager, viewer = new_client(), new_client()
    manager.post("/connexion", data={"email": "gil@test.fr", "password": "motdepasse-solide"})
    viewer.post("/connexion", data={"email": "vic@test.fr", "password": "motdepasse-solide"})
    assert manager.get(URL).status_code == 200 and "Importer des locations Getaround" in manager.get("/locations").text
    assert viewer.get(URL).status_code == 403 and post(viewer, [row()]).status_code == 403
    assert "Importer des locations Getaround" not in viewer.get("/locations").text
    assert new_client().get(URL).status_code == 303
    assert db.query(Booking).count() == 0


def test_future_api_sync_recognises_imported_rentals(client, fleet, db, monkeypatch):
    """La synchro API retombe sur la location importée (même getaround_id) au lieu d'en créer une seconde."""
    from .test_integrations import FakeGetaround
    post(client, [row(getaround_id="77", locataire="Léa M.")])
    fake = FakeGetaround(now() - timedelta(hours=2), now() + timedelta(hours=20))
    monkeypatch.setattr(getaround, "client_for", lambda o: getaround.GetaroundClient("TOKEN", transport=httpx.MockTransport(fake)))
    org = db.query(Organization).one()
    settings.update(org, {"getaround_token": "TOKEN"})
    db.commit()
    stats = getaround.sync_all(db, org)
    assert stats["rentals"] == 1 and db.query(Booking).count() == 1
    b = db.query(Booking).one()
    assert b.price_ht == 120.50 and b.customer.last_name == "Martin" and b.renter_label.startswith("Léa")


def test_message_sync_survives_rentals_unknown_to_the_api(client, fleet, db, monkeypatch):
    """Une location importée que l'API ne connaît pas ne doit pas faire échouer toute la synchro."""
    post(client, [row(getaround_id="424242")])
    empty = httpx.MockTransport(lambda r: httpx.Response(404) if "424242" in r.url.path else httpx.Response(200, json=[]))
    monkeypatch.setattr(getaround, "client_for", lambda o: getaround.GetaroundClient("TOKEN", transport=empty))
    org = db.query(Organization).one()
    settings.update(org, {"getaround_token": "TOKEN"})
    db.commit()
    assert getaround.sync_all(db, org)["messages"] == 0

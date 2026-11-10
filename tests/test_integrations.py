"""Getaround (API simulée), assistant, contraventions, facturation Pennylane, extension."""
import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest

from app.models import Booking, Customer, Fine, Invoice, Message, Organization, Vehicle
from app.services import assistant, fines, getaround, invoicing, settings
from app.timeutil import now, to_iso

from .conftest import new_client


class FakeGetaround:
    """Imite l'Owner API : mêmes routes et mêmes formes de réponse que la spécification."""

    def __init__(self, start, end):
        self.sent, self.unavailabilities = [], []
        self.rental = {"id": 77, "car_id": 9, "user_id": 501, "starts_at": to_iso(start), "ends_at": to_iso(end),
                       "booked_at": to_iso(start), "price": 12050, "insurance_fee": 800}
        self.messages = {1: {"id": 1, "rental_id": 77, "sending_user_id": 501, "sent_at": to_iso(start), "content": "Bonjour, quelle est la hauteur du véhicule ?"},
                         2: {"id": 2, "rental_id": 77, "sending_user_id": 999, "sent_at": to_iso(start), "content": "2,50 m."}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer TOKEN"
        path, body = request.url.path.removeprefix("/owner/v1"), json.loads(request.content or b"{}")
        if path == "/cars.json":
            return httpx.Response(200, json=[{"id": 9}])
        if path == "/cars/9.json":
            return httpx.Response(200, json={"id": 9, "state": "active", "plate_number": "AB123CD", "brand": "Renault", "model": "Master"})
        if path == "/rentals.json":
            return httpx.Response(200, json=[{"id": 77}])
        if path == "/rentals/77.json":
            return httpx.Response(200, json=self.rental)
        if path == "/users/501.json":
            return httpx.Response(200, json={"id": 501, "first_name": "Léa", "last_name": "Martin", "phone_number": "+33600000000",
                                             "address_line1": "3 rue de la Paix", "address_line2": "", "postal_code": "69002", "city": "Lyon",
                                             "country": "France", "birth_date": "1990-04-12", "license_country": "France",
                                             "license_first_issue_date": "2009-06-01", "license_number": "09AB12345"})
        if path == "/rentals/77/messages.json" and request.method == "GET":
            return httpx.Response(200, json=[{"id": i} for i in self.messages])
        if path == "/rentals/77/messages.json":
            self.sent.append(body["content"])
            return httpx.Response(201, json={"id": 100 + len(self.sent), "content": body["content"]})
        if path.startswith("/rentals/77/messages/"):
            return httpx.Response(200, json=self.messages[int(path.split("/")[-1].removesuffix(".json"))])
        if path == "/cars/9/unavailabilities.json":
            self.unavailabilities.append((request.method, body))
            return httpx.Response(201 if request.method == "POST" else 204)
        return httpx.Response(404)


@pytest.fixture
def connected(client, fleet, db, monkeypatch):
    """Société avec jeton Getaround ; les appels réseau partent vers FakeGetaround."""
    org = db.query(Organization).one()
    settings.update(org, {"getaround_token": "TOKEN", "getaround_webhook_secret": "s3cret"})
    db.commit()
    fake = FakeGetaround(now() - timedelta(hours=2), now() + timedelta(hours=20))
    monkeypatch.setattr(getaround, "client_for", lambda o: getaround.GetaroundClient("TOKEN", transport=httpx.MockTransport(fake)))
    return org, fake


def test_secrets_are_encrypted_at_rest(connected, db):
    org, _ = connected
    assert "TOKEN" not in json.dumps(org.settings) and settings.get(org, "getaround_token") == "TOKEN"
    settings.update(org, {"getaround_token": ""})  # champ vide = secret conservé
    assert settings.get(org, "getaround_token") == "TOKEN"


def test_sync_imports_rentals_drivers_and_messages(connected, db):
    org, _ = connected
    stats = getaround.sync_all(db, org)
    assert stats == {"cars": 1, "rentals": 1, "messages": 1}
    b = db.query(Booking).filter_by(source="getaround").one()
    assert b.reference == "GA-77" and b.price_ht == 120.50 and b.status == "in_progress"
    assert b.vehicle.getaround_car_id == 9  # rattaché par la plaque AB-123-CD
    assert (b.customer.last_name, b.customer.license_number, str(b.customer.birth_date)) == ("Martin", "09AB12345", "1990-04-12")
    assert {(m.direction, m.status) for m in db.query(Message)} == {("in", "new"), ("out", "sent")}
    assert getaround.sync_all(db, org)["messages"] == 0  # idempotent
    assert db.query(Booking).count() == 1 and db.query(Customer).filter_by(getaround_user_id=501).count() == 1


def test_webhook_signature_and_idempotence(connected, db):
    org, _ = connected
    body = json.dumps({"type": "rental.booked", "data": {"rental_id": 77}, "occurred_at": "2026-01-01T10:00:00Z"}).encode()
    good = "sha1=" + hmac.new(b"s3cret", body, hashlib.sha1).hexdigest()
    c = new_client()
    url = f"/webhooks/getaround/{org.webhook_key}"
    assert c.post(url, content=body, headers={"X-Drivy-Signature": "sha1=bad"}).status_code == 401
    assert c.post(url, content=body).status_code == 401
    assert c.post("/webhooks/getaround/inconnu", content=body, headers={"X-Drivy-Signature": good}).status_code == 404
    assert c.post(url, content=body, headers={"X-Drivy-Signature": good}).json() == {"status": "ok"}
    assert db.query(Booking).filter_by(external_id="77").count() == 1
    assert c.post(url, content=body, headers={"X-Drivy-Signature": good}).json() == {"status": "duplicate"}
    cancel = json.dumps({"type": "rental.canceled", "data": {"rental_id": 77}, "occurred_at": "2026-01-02T10:00:00Z"}).encode()
    c.post(url, content=cancel, headers={"X-Drivy-Signature": "sha1=" + hmac.new(b"s3cret", cancel, hashlib.sha1).hexdigest()})
    assert db.query(Booking).filter_by(external_id="77").one().status == "cancelled"


@pytest.mark.parametrize("mode,decision,status,sent", [
    ("auto", assistant.Decision(action="reply", reply="Il mesure 2,50 m.", reason="factuel"), "auto_replied", 1),
    ("draft", assistant.Decision(action="reply", reply="Il mesure 2,50 m.", reason="factuel"), "draft", 0),
    ("auto", assistant.Decision(action="escalate", reply="", reason="remboursement demandé"), "escalated", 0),
])
def test_assistant_modes(connected, db, monkeypatch, mode, decision, status, sent):
    org, fake = connected
    settings.update(org, {"anthropic_api_key": "sk-test", "assistant_mode": mode})
    db.commit()
    monkeypatch.setattr(assistant, "decide", lambda *a: decision)
    getaround.sync_all(db, org)
    msg = db.query(Message).filter_by(direction="in").one()
    assert msg.status == status and len(fake.sent) == sent and msg.ai_reason == decision.reason
    if status == "auto_replied":
        assert db.query(Message).filter_by(by_ai=True).one().content == "Il mesure 2,50 m."


def test_assistant_prompt_contains_rental_context_only(connected, db):
    org, _ = connected
    settings.update(org, {"assistant_instructions": "Hauteur 2,50 m."})
    getaround.sync_all(db, org)
    msg = db.query(Message).filter_by(direction="in").one()
    text = assistant._context(org, msg, db.query(Message).all())
    assert "Hauteur 2,50 m." in text and "quelle est la hauteur" in text and "AB-123-CD" in text
    assert "4821" not in text  # le code de la boîte à clés ne part jamais vers le modèle


def test_manual_reply_is_sent_to_getaround(client, connected, db):
    org, fake = connected
    getaround.sync_all(db, org)
    msg = db.query(Message).filter_by(direction="in").one()
    client.post(f"/messages/{msg.id}/repondre", data={"text": "Bonjour, 2,50 m."})
    db.refresh(msg)
    assert fake.sent == ["Bonjour, 2,50 m."] and msg.status == "handled"


def test_direct_booking_blocks_getaround_calendar(client, connected, db):
    org, fake = connected
    getaround.sync_cars(db, org, getaround.client_for(org))
    vehicle, customer = db.query(Vehicle).one(), db.query(Customer).first()
    start = (now() + timedelta(days=5)).replace(hour=8, minute=10)
    client.post("/locations", data={"vehicle_id": vehicle.id, "customer_id": customer.id, "starts_at": start.isoformat(),
                                    "ends_at": (start + timedelta(hours=4)).isoformat()})
    method, body = fake.unavailabilities[0]
    assert method == "POST" and body["reason"] == "booked"
    assert body["starts_at"][14:16] == "00" and body["ends_at"][14:16] == "30"  # arrondi à 30 minutes, vers l'extérieur


# --- contraventions ---
def make_pdf(path, lines):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i, line in enumerate(lines):
        c.drawString(60, 780 - 18 * i, line)
    c.save()


def test_fine_is_read_and_matched_to_the_rental(connected, db, tmp_path):
    org, _ = connected
    getaround.sync_all(db, org)
    when = now() + timedelta(minutes=30)
    inbox = fines.inbox(org.id)
    make_pdf(inbox / "avis.pdf", ["AVIS DE CONTRAVENTION", "Numero de l'avis : 4012345678", "Immatriculation : AB-123-CD",
                                  f"Date de l'infraction : {when:%d/%m/%Y} a {when:%Hh%M}", "Montant de l'amende forfaitaire : 135 euros"])
    assert fines.scan_inbox(db, org) == 1 and list(inbox.glob("*.pdf")) == []
    fine = db.query(Fine).one()
    assert (fine.kind, fine.notice_number, fine.plate, fine.amount, fine.extraction) == ("amende", "4012345678", "AB-123-CD", 135.0, "regex")
    assert fine.status == "matched" and fine.booking.reference == "GA-77"
    data = fines.designation(org, fine)
    assert data["driver"]["last_name"] == "Martin" and data["driver"]["license_number"] == "09AB12345" and data["driver"]["birth_date"] == "12/04/1990"
    # même fichier redéposé : ignoré
    make_pdf(inbox / "copie.pdf", ["x"])
    (inbox / "copie.pdf").write_bytes((__import__("app.config").config.DATA_DIR / fine.file_path).read_bytes())
    assert fines.scan_inbox(db, org) == 0 and db.query(Fine).count() == 1


def test_fine_outside_any_rental_is_unmatched_and_fps_detected(connected, db):
    org, _ = connected
    getaround.sync_all(db, org)
    make_pdf(fines.inbox(org.id) / "fps.pdf", ["Avis de paiement - Forfait post-stationnement", "N 26069123456789012345",
                                               "Vehicule AB 123 CD", "Constat du 03/01/2020 a 10:15", "Montant : 35,00 €"])
    fines.scan_inbox(db, org)
    fine = db.query(Fine).one()
    assert (fine.kind, fine.status, fine.amount, fine.booking_id) == ("fps", "unmatched", 35.0, None)
    assert fine.vehicle is not None


def test_unreadable_fine_goes_to_review_and_can_be_fixed(client, connected, db):
    org, _ = connected
    getaround.sync_all(db, org)
    make_pdf(fines.inbox(org.id) / "flou.pdf", ["document illisible"])
    fines.scan_inbox(db, org)
    fine = db.query(Fine).one()
    assert fine.status == "to_review"
    client.post(f"/contraventions/{fine.id}", data={"kind": "amende", "notice_number": "123", "plate": "ab123cd",
                                                    "offense_at": now().isoformat(timespec="minutes"), "amount": "90"})
    db.refresh(fine)
    assert fine.status == "matched" and fine.booking.reference == "GA-77"


def test_extension_api_requires_token_and_lists_matched_fines(client, connected, db):
    org, _ = connected
    getaround.sync_all(db, org)
    b = db.query(Booking).one()
    db.add(Fine(org_id=org.id, file_path="", file_hash="h", kind="amende", notice_number="42", plate="AB-123-CD",
                offense_at=now(), vehicle_id=b.vehicle_id, booking_id=b.id, status="matched"))
    db.commit()
    anon = new_client()
    assert anon.get("/api/ext/fines").status_code == 401
    assert anon.get("/api/ext/fines", headers={"Authorization": "Bearer sf_faux"}).status_code == 401
    client.post("/reglages-jeton")
    page = client.get("/reglages").text
    token = "sf_" + page.split('data-copy="sf_')[1].split('"')[0]
    assert token not in client.get("/reglages").text  # affiché une seule fois
    auth = {"Authorization": f"Bearer {token}", "Origin": "chrome-extension://abc"}
    data = anon.get("/api/ext/fines", headers=auth)
    assert data.headers["access-control-allow-origin"] == "*"
    assert [f["notice_number"] for f in data.json()["fines"]] == ["42"]
    assert data.json()["fines"][0]["driver"]["first_name"] == "Léa"
    assert anon.post(f"/api/ext/fines/{db.query(Fine).one().id}/done", headers=auth).json() == {"status": "done"}
    assert anon.get("/api/ext/fines", headers=auth).json()["fines"] == []


# --- facturation ---
def test_fps_is_reinvoiced_with_admin_fee(client, fleet, db):
    vehicle, customer = fleet
    start = now() - timedelta(days=1)
    client.post("/locations", data={"vehicle_id": vehicle.id, "customer_id": customer.id, "starts_at": start.isoformat(),
                                    "ends_at": (start + timedelta(hours=6)).isoformat()})
    b = db.query(Booking).one()
    db.add(Fine(org_id=b.org_id, file_path="", file_hash="h", kind="fps", notice_number="555", plate=vehicle.plate, amount=35,
                offense_at=start + timedelta(hours=1), vehicle_id=vehicle.id, booking_id=b.id, status="matched"))
    db.commit()
    client.post(f"/contraventions/{db.query(Fine).one().id}/refacturer")
    inv = db.query(Invoice).one()
    assert (inv.amount_ht, inv.amount_ttc) == (60.0, 65.0)  # 35 € de débours sans TVA + 25 € HT de frais (30 € TTC)
    assert db.query(Fine).one().status == "done"


def test_pennylane_invoice_payload(fleet, db):
    vehicle, customer = fleet
    org = db.query(Organization).one()
    settings.update(org, {"invoice_provider": "pennylane", "pennylane_token": "PL"})
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer PL"
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/company_customers"):
            return httpx.Response(201, json={"id": 314})
        return httpx.Response(201, json={"id": 2718, "invoice_number": None, "public_file_url": "https://app.pennylane.com/f/x.pdf"})

    invoicing.PROVIDERS["pennylane"].transport = httpx.MockTransport(handler)
    lines = [{"label": "Location", "quantity": 1, "unit_price": 90.0, "vat_rate": 20}]
    customer = db.get(Customer, customer.id)
    inv = invoicing.create_invoice(db, org, customer, lines, "Location test")
    invoicing.create_invoice(db, org, customer, lines, "Location test 2")
    paths = [p for p, _ in calls]
    assert paths.count("/api/external/v2/company_customers") == 1  # client créé une seule fois
    body = calls[1][1]
    assert body["customer_id"] == 314 and body["draft"] is True
    assert body["invoice_lines"][0] == {"label": "Location", "quantity": 1, "unit": "piece", "raw_currency_unit_price": "90.00", "vat_rate": "FR_200"}
    assert (inv.provider, inv.external_id, inv.status, inv.amount_ttc) == ("pennylane", "2718", "draft", 108.0)

    invoicing.PROVIDERS["pennylane"].transport = httpx.MockTransport(lambda r: httpx.Response(422, text="invalid"))
    with pytest.raises(invoicing.InvoicingError):
        invoicing.create_invoice(db, org, customer, lines, "KO")

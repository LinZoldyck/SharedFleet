"""Envoi du code d'accès par e-mail (serveur SMTP simulé)."""
import json
import smtplib
from datetime import timedelta

import pytest

from app.models import Booking, Organization
from app.services import bookings as svc
from app.services import email, scheduler, settings
from app.timeutil import now


class FakeSMTP:
    """Remplace smtplib.SMTP : enregistre la connexion et les messages au lieu de les envoyer."""
    sent, log, fail = [], [], None

    def __init__(self, host, port, timeout=None, **kw):
        FakeSMTP.log.append(("connect", host, port, kw.get("context") is not None))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.log.append(("starttls",))

    def login(self, user, password):
        if FakeSMTP.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        FakeSMTP.log.append(("login", user, password))

    def send_message(self, msg):
        if FakeSMTP.fail == "network":
            raise OSError("Connection timed out")
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch, db):
    FakeSMTP.sent, FakeSMTP.log, FakeSMTP.fail = [], [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    org = db.query(Organization).one()
    settings.update(org, {"email_provider": "smtp", "smtp_host": "smtp.example.org", "smtp_port": "", "smtp_security": "starttls",
                          "smtp_user": "location@example.org", "smtp_password": "app-password", "email_from": "location@example.org",
                          "email_from_name": "", "company_email": "contact@utilitaires.fr"})
    db.commit()
    return FakeSMTP


@pytest.fixture
def sms(monkeypatch):
    sent = []
    monkeypatch.setattr(svc, "send_sms", lambda org, to, text: sent.append(to) or True)
    return sent


def make_booking(client, db, vehicle, customer, minutes=10):
    start = now() + timedelta(minutes=minutes)
    client.post("/locations", data={"vehicle_id": vehicle.id, "customer_id": customer.id, "starts_at": start.isoformat(),
                                    "ends_at": (start + timedelta(hours=6)).isoformat()})
    return db.query(Booking).order_by(Booking.id.desc()).first()


def test_code_goes_out_by_sms_and_email(client, fleet, db, smtp, sms):
    vehicle, customer = fleet
    b = make_booking(client, db, vehicle, customer)
    assert scheduler.send_due_codes() == 1
    db.refresh(b)
    assert b.code_sent_at and sms == ["06 12 34 56 78"]
    assert ("connect", "smtp.example.org", 587, False) in smtp.log and ("starttls",) in smtp.log
    assert ("login", "location@example.org", "app-password") in smtp.log
    msg = smtp.sent[0]
    assert msg["To"] == "Inès Moreau <ines@lumen.fr>" and "Utilitaires Test" in msg["From"]
    assert msg["Reply-To"] == "contact@utilitaires.fr" and "Votre code d'accès" in msg["Subject"]
    text, html = msg.get_body(("plain",)).get_content(), msg.get_body(("html",)).get_content()
    for body in (text, html):
        assert b.access_code in body and "http://testserver/acces" in body
        assert vehicle.lock_code not in body  # le code de la boîte à clés n'est jamais envoyé
    assert scheduler.send_due_codes() == 0  # pas de second envoi


def test_email_only_customer_gets_the_code(client, fleet, db, smtp, sms):
    vehicle, customer = fleet
    customer.phone = ""
    db.commit()
    b = make_booking(client, db, vehicle, customer)
    assert svc.send_access_code(db, db.query(Organization).one(), b) == {"email": "ok"}
    assert sms == [] and len(smtp.sent) == 1 and b.code_sent_at


def test_channels_follow_settings(client, fleet, db, smtp, sms):
    vehicle, customer = fleet
    org = db.query(Organization).one()
    b = make_booking(client, db, vehicle, customer, minutes=300)
    client.post("/reglages/code", data={"code_by_email": "on"})  # SMS décoché
    db.refresh(org)
    assert svc.send_access_code(db, org, b) == {"email": "ok"} and sms == []
    settings.update(org, {"email_provider": "off", "code_by_sms": True})
    assert svc.send_access_code(db, org, b) == {"sms": "ok"}  # e-mail désactivé : ignoré sans erreur


def test_ssl_port_465(client, fleet, db, smtp, sms):
    vehicle, customer = fleet
    org = db.query(Organization).one()
    settings.update(org, {"smtp_security": "ssl"})
    db.commit()
    svc.send_access_code(db, org, make_booking(client, db, vehicle, customer), channels=("email",))
    assert ("connect", "smtp.example.org", 465, True) in smtp.log and ("starttls",) not in smtp.log


def test_failure_is_reported_and_retried_later(client, fleet, db, smtp, monkeypatch):
    vehicle, customer = fleet
    monkeypatch.setattr(svc, "send_sms", lambda *a: False)
    smtp.fail = "auth"
    b = make_booking(client, db, vehicle, customer)
    assert scheduler.send_due_codes() == 1
    db.refresh(b)
    assert b.code_sent_at is None  # rien n'est parti
    history = svc.code_history(db, b)
    assert {h["channel"] for h in history} == {"sms", "email"} and not any(h["ok"] for h in history)
    assert "mot de passe d'application" in next(h for h in history if h["channel"] == "email")["error"]
    assert scheduler.send_due_codes() == 0  # pas de nouvelle tentative avant 10 minutes
    page = client.get(f"/locations/{b.id}").text
    assert "E-mail : échec" in page or "e-mail : échec" in page


def test_manual_send_buttons(client, fleet, db, smtp, sms):
    vehicle, customer = fleet
    b = make_booking(client, db, vehicle, customer, minutes=600)
    page = client.get(f"/locations/{b.id}").text
    assert "Envoyer par SMS" in page and "Envoyer par e-mail" in page
    client.post(f"/locations/{b.id}/code", data={"channel": "email"})
    assert len(smtp.sent) == 1 and sms == []
    assert "Code d&#39;accès envoyé par e-mail (ines@lumen.fr)" in client.get(f"/locations/{b.id}").text
    smtp.fail = "network"
    client.post(f"/locations/{b.id}/code", data={"channel": "email"})
    assert "Connection timed out" in client.get(f"/locations/{b.id}").text


def test_settings_page_test_email_and_secret_storage(client, db, smtp):
    org = db.query(Organization).one()
    assert "app-password" not in json.dumps(org.settings)  # mot de passe chiffré en base
    page = client.get("/reglages").text
    assert 'id="email"' in page and "app-password" not in page
    client.post("/reglages-email-test", data={"to": "moi@example.org"})
    assert smtp.sent[-1]["To"] == "moi@example.org" and "test" in smtp.sent[-1]["Subject"]
    client.post("/reglages-email-test", data={"to": "pas-une-adresse"})
    assert len(smtp.sent) == 1


def test_logo_is_embedded(client, fleet, db, smtp, sms):
    from .conftest import photo
    client.post("/reglages-logo", files={"logo": ("logo.png", photo()[1], "image/jpeg")})
    vehicle, customer = fleet
    svc.send_access_code(db, db.query(Organization).one(), make_booking(client, db, vehicle, customer), channels=("email",))
    msg = smtp.sent[0]
    assert 'src="cid:logo"' in msg.get_body(("html",)).get_content()
    assert any(part.get("Content-ID") == "<logo>" for part in msg.walk())


def test_header_injection_is_impossible(client, fleet, db, smtp, sms):
    """Une adresse contenant un retour à la ligne (pour ajouter un destinataire caché) est ignorée."""
    vehicle, customer = fleet
    customer.email = "ines@lumen.fr\nBcc: autre@example.org"
    db.commit()
    assert not email.looks_valid(customer.email)
    assert svc.send_access_code(db, db.query(Organization).one(), make_booking(client, db, vehicle, customer)) == {"sms": "ok"}
    assert smtp.sent == []

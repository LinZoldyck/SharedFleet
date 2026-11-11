"""Connexion OAuth Microsoft (Outlook.com) : connexion par code, renouvellement des jetons, envoi SMTP XOAUTH2."""
import base64
import json
import smtplib
import time
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from app.models import Booking, Organization
from app.services import bookings as svc
from app.services import email, microsoft, scheduler, settings
from app.timeutil import now

ID_TOKEN = "x." + base64.urlsafe_b64encode(json.dumps({"preferred_username": "loueur@outlook.fr"}).encode()).decode().rstrip("=") + ".sig"


class FakeMicrosoft:
    """Imite login.microsoftonline.com : /devicecode et /token (code, puis renouvellement avec rotation)."""

    def __init__(self):
        self.calls, self.approved, self.refresh_ok, self.n = [], False, True, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((request.url.path, form))
        assert form["client_id"] == "client-123"
        if request.url.path == "/consumers/oauth2/v2.0/devicecode":
            assert "https://outlook.office.com/SMTP.Send" in form["scope"] and "offline_access" in form["scope"]
            return httpx.Response(200, json={"device_code": "DEV", "user_code": "ABCD-EFGH", "interval": 5, "expires_in": 900,
                                             "verification_uri": "https://microsoft.com/devicelogin"})
        if form["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code":
            assert form["device_code"] == "DEV"
            if not self.approved:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "AT0", "refresh_token": "RT0", "expires_in": 3600, "id_token": ID_TOKEN})
        if form["grant_type"] == "refresh_token":
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS70000: expired"})
            assert form["refresh_token"] == f"RT{self.n}"
            self.n += 1
            return httpx.Response(200, json={"access_token": f"AT{self.n}", "refresh_token": f"RT{self.n}", "expires_in": 3600})
        return httpx.Response(404)


@pytest.fixture
def ms(client, db, monkeypatch):
    fake = FakeMicrosoft()
    monkeypatch.setattr(microsoft, "_transport", httpx.MockTransport(fake))
    microsoft._access.clear()
    microsoft._pending.clear()
    client.post("/reglages/email", data={"email_provider": "microsoft", "ms_client_id": "client-123", "ms_tenant": "consumers"})
    return fake


def connect(client, fake):
    page = client.post("/reglages-microsoft/connexion").text
    assert "ABCD-EFGH" in page and "https://microsoft.com/devicelogin" in page
    assert client.post("/reglages-microsoft/verifier").json() == {"status": "pending"}
    fake.approved = True
    assert client.post("/reglages-microsoft/verifier").json() == {"status": "ok", "account": "loueur@outlook.fr"}


def test_device_login_connects_and_stores_tokens_encrypted(client, db, ms):
    connect(client, ms)
    org = db.query(Organization).one()
    db.refresh(org)
    assert settings.get(org, "ms_account") == "loueur@outlook.fr" and settings.get(org, "ms_refresh_token") == "RT0"
    assert "RT0" not in json.dumps(org.settings)  # chiffré en base
    assert "connecté" in client.get("/reglages").text and "loueur@outlook.fr" in client.get("/reglages").text
    assert email.enabled(org) and email.sender(org) == "loueur@outlook.fr"


def test_login_requires_client_id_and_admin(client, db, ms):
    client.post("/reglages/email", data={"email_provider": "microsoft", "ms_client_id": "", "ms_tenant": "consumers"})
    client.post("/reglages-microsoft/connexion")
    assert "identifiant d&#39;application" in client.get("/reglages").text
    from .conftest import new_client
    assert new_client().post("/reglages-microsoft/verifier").status_code == 303  # non connecté → page de connexion


def test_refresh_rotates_tokens_and_caches_access_token(client, db, ms):
    connect(client, ms)
    org = db.query(Organization).one()
    db.refresh(org)
    assert microsoft.access_token(org) == "AT0"  # jeton encore valide : pas d'appel
    microsoft._access[org.id] = ("AT0", time.time() - 1)  # expiré
    assert microsoft.access_token(org) == "AT1"
    db.refresh(org)
    assert settings.get(org, "ms_refresh_token") == "RT1"  # nouveau jeton de renouvellement conservé
    scheduler.refresh_microsoft()  # maintien quotidien de la connexion
    db.refresh(org)
    assert settings.get(org, "ms_refresh_token") == "RT2"


def test_expired_connection_is_reported(client, db, ms):
    connect(client, ms)
    org = db.query(Organization).one()
    ms.refresh_ok = False
    microsoft._access.clear()
    with pytest.raises(microsoft.MicrosoftError, match="reconnectez"):
        microsoft.access_token(org)
    assert "reconnectez le compte" in client.get("/reglages").text


def test_disconnect(client, db, ms):
    connect(client, ms)
    client.post("/reglages-microsoft/deconnexion")
    org = db.query(Organization).one()
    db.refresh(org)
    assert not microsoft.connected(org) and not email.enabled(org)


def test_public_client_flow_hint(client, db, monkeypatch):
    monkeypatch.setattr(microsoft, "_transport", httpx.MockTransport(lambda r: httpx.Response(400, json={
        "error": "invalid_client", "error_description": "AADSTS7000218: The request body must contain client_assertion"})))
    client.post("/reglages/email", data={"email_provider": "microsoft", "ms_client_id": "client-123", "ms_tenant": "consumers"})
    client.post("/reglages-microsoft/connexion")
    assert "flux de clients publics" in client.get("/reglages").text


# --- envoi réel en SMTP + XOAUTH2 contre un serveur local (aiosmtpd) ---
aiosmtpd = pytest.importorskip("aiosmtpd.controller")
from aiosmtpd.smtp import AuthResult  # noqa: E402


class XOAuthServer:
    def __init__(self):
        self.auth, self.messages = [], []

    async def auth_XOAUTH2(self, server, args):
        # args = ["XOAUTH2", <réponse initiale base64>]
        initial = base64.b64decode(args[1]).decode() if len(args) > 1 else (await server.challenge_auth("")).decode()
        self.auth.append(initial)
        if "auth=Bearer AT" in initial:
            return AuthResult(success=True, auth_data=initial)
        # Comportement de Microsoft : 334 + JSON d'erreur, puis 535 après la ligne vide du client
        await server.challenge_auth('{"status":"401","schemes":"bearer","scope":"https://outlook.office.com/SMTP.Send"}')
        return AuthResult(success=False, handled=False)

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope)
        return "250 OK"


@pytest.fixture
def smtp_server(monkeypatch):
    handler = XOAuthServer()
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    controller = aiosmtpd.Controller(handler, hostname="127.0.0.1", port=port, auth_require_tls=False,
                                     auth_exclude_mechanism=["LOGIN", "PLAIN"])
    controller.start()
    real = smtplib.SMTP

    class LocalSMTP(real):  # même dialogue SMTP, mais vers le serveur local et sans TLS
        def __init__(self, host, _port, timeout=None):
            assert host == "smtp-mail.outlook.com" and _port == 587
            super().__init__("127.0.0.1", port, timeout=timeout)

        def starttls(self, *a, **kw):  # comme le vrai : la session doit être réinitialisée et l'EHLO refait
            self.ehlo_resp = None
            self.esmtp_features = {}
            self.does_esmtp = False
            return (220, b"ok")

    monkeypatch.setattr(smtplib, "SMTP", LocalSMTP)
    yield handler
    controller.stop()


def test_access_code_email_goes_through_xoauth2(client, fleet, db, ms, smtp_server, monkeypatch):
    connect(client, ms)
    monkeypatch.setattr(svc, "send_sms", lambda *a: True)
    vehicle, customer = fleet
    start = now() + timedelta(minutes=10)
    client.post("/locations", data={"vehicle_id": vehicle.id, "customer_id": customer.id, "starts_at": start.isoformat(),
                                    "ends_at": (start + timedelta(hours=5)).isoformat()})
    assert scheduler.send_due_codes() == 1
    assert smtp_server.auth == ["user=loueur@outlook.fr\x01auth=Bearer AT0\x01\x01"]
    envelope = smtp_server.messages[0]
    assert envelope.mail_from == "loueur@outlook.fr" and envelope.rcpt_tos == ["ines@lumen.fr"]
    assert db.query(Booking).one().access_code in envelope.content.decode()
    assert "From: Utilitaires Test <loueur@outlook.fr>" in envelope.content.decode()


def test_rejected_token_gives_clear_error(client, fleet, db, ms, smtp_server):
    connect(client, ms)
    org = db.query(Organization).one()
    microsoft._access[org.id] = ("MAUVAIS", time.time() + 3600)
    msg = email.build(org, "moi@example.org", "", "Test", "texte")
    with pytest.raises(email.EmailError, match="Microsoft a refusé l'identification"):
        email.send(org, msg)
    assert smtp_server.messages == []

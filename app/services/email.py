"""Envoi d'e-mails. Fournisseurs : off (désactivé), console (journal, pour tester), SMTP (identifiant + mot de passe)
ou microsoft (Outlook.com / Microsoft 365 via OAuth, voir microsoft.py).

SMTP couvre presque tous les services : Gmail / Google Workspace, Outlook / Microsoft 365, OVH, Brevo,
Mailjet, Scaleway… L'envoi passe par la bibliothèque standard (smtplib), sans dépendance supplémentaire.
"""
import base64
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid

import httpx

from ..models import Organization
from . import media, microsoft, settings

log = logging.getLogger("sharedfleet.email")

SECURITY = {"starttls": "STARTTLS (port 587, le plus courant)", "ssl": "SSL/TLS (port 465)", "none": "Aucune (port 25, réseau local)"}


class EmailError(Exception):
    pass


def enabled(org: Organization) -> bool:
    provider = settings.get(org, "email_provider")
    return provider in ("console", "smtp") or (provider == "microsoft" and microsoft.connected(org))


def sender(org: Organization) -> str:
    """Adresse d'expédition. Avec Microsoft, c'est obligatoirement celle du compte connecté (sinon refus « SendAsDenied »)."""
    if settings.get(org, "email_provider") == "microsoft":
        return settings.get(org, "ms_account")
    return settings.get(org, "email_from")


def looks_valid(address: str) -> bool:
    address = (address or "").strip()
    return "@" in address and "." in address.rsplit("@", 1)[-1] and " " not in address and "\n" not in address


def build(org: Organization, to: str, to_name: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    """Message multipart (texte + HTML). Le logo de la société est joint en image intégrée (cid:logo)."""
    from_address = sender(org)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.get(org, "email_from_name") or org.name, from_address))
    msg["To"] = formataddr((to_name, to.strip())) if to_name else to.strip()
    reply_to = settings.get(org, "company_email")
    if looks_valid(reply_to) and reply_to != from_address:
        msg["Reply-To"] = reply_to
    msg["Message-ID"] = make_msgid(domain=from_address.rsplit("@", 1)[-1] if "@" in from_address else None)
    msg.set_content(text)
    if html:
        logo = media.absolute(settings.get(org, "logo_path"))
        msg.add_alternative(html.replace("{{logo}}", '<img src="cid:logo" alt="" style="max-height:48px;max-width:180px">'
                                         if logo else ""), subtype="html")
        if logo:
            subtype = "png" if logo.suffix == ".png" else "jpeg"
            msg.get_payload()[1].add_related(logo.read_bytes(), "image", subtype, cid="<logo>")
    return msg


def send(org: Organization, msg: EmailMessage) -> None:
    """Envoie le message ou lève EmailError avec une explication lisible."""
    provider = settings.get(org, "email_provider")
    if provider == "console":
        log.warning("E-mail (mode test, non envoyé) → %s : %s\n%s", msg["To"], msg["Subject"], msg.get_body(("plain",)).get_content())
        return
    if provider == "microsoft":
        return _send_microsoft(org, msg)
    if provider != "smtp":
        raise EmailError("L'envoi d'e-mails est désactivé (Réglages → E-mail).")
    host, security = settings.get(org, "smtp_host"), settings.get(org, "smtp_security")
    if not host or not looks_valid(settings.get(org, "email_from")):
        raise EmailError("Serveur SMTP ou adresse d'expédition non renseigné (Réglages → E-mail).")
    port = int(settings.get(org, "smtp_port") or {"ssl": 465, "none": 25}.get(security, 587))
    user, password = settings.get(org, "smtp_user"), settings.get(org, "smtp_password")
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            if security == "starttls":
                server.starttls(context=context)
            if user:
                server.login(user, password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        if "basic auth" in detail.lower() or "5.7.139" in detail or "5.7.30" in detail:
            raise EmailError("Microsoft refuse la connexion par mot de passe (Outlook.com / Hotmail, ou Microsoft 365 "
                             "avec SMTP AUTH désactivé). Utilisez un autre service d'envoi.") from exc
        raise EmailError("Identifiant ou mot de passe SMTP refusé. Avec Gmail, utilisez un « mot de passe d'application ».") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise EmailError(f"Adresse refusée par le serveur : {msg['To']}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Envoi impossible via {host}:{port} — {exc}") from exc


def graph_payload(msg: EmailMessage) -> dict:
    """Convertit le message en requête Microsoft Graph sendMail ; le logo devient une pièce jointe intégrée (cid:logo)."""
    def recipients(header):
        return [{"emailAddress": {"address": addr, **({"name": name} if name else {})}}
                for name, addr in getaddresses([msg.get(header, "")]) if addr]

    html, text = msg.get_body(("html",)), msg.get_body(("plain",))
    body = {"contentType": "HTML", "content": html.get_content()} if html else {"contentType": "Text", "content": text.get_content()}
    attachments = []
    for part in msg.walk():
        cid = part.get("Content-ID")
        if cid and part.get_content_maintype() == "image":
            attachments.append({"@odata.type": "#microsoft.graph.fileAttachment", "name": cid.strip("<>") + "." + part.get_content_subtype(),
                                "contentType": part.get_content_type(), "isInline": True, "contentId": cid.strip("<>"),
                                "contentBytes": base64.b64encode(part.get_payload(decode=True)).decode()})
    message = {"subject": msg["Subject"], "body": body, "toRecipients": recipients("To")}
    if msg.get("Reply-To"):
        message["replyTo"] = recipients("Reply-To")
    if attachments:
        message["attachments"] = attachments
    return {"message": message, "saveToSentItems": True}


def _send_graph(org: Organization, msg: EmailMessage, token: str) -> None:
    """Envoi par l'API Microsoft Graph (HTTPS). L'e-mail apparaît aussi dans « Éléments envoyés » d'Outlook."""
    try:
        with httpx.Client(transport=microsoft._transport, timeout=30) as http:
            r = http.post(microsoft.GRAPH_SEND_URL, json=graph_payload(msg), headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise EmailError(f"Microsoft Graph injoignable : {exc}") from exc
    if r.is_success:  # 202 Accepted
        return
    try:
        error = r.json().get("error", {})
    except ValueError:
        error = {}
    code, detail = error.get("code", ""), (error.get("message") or r.text)[:200]
    if r.status_code == 401:
        raise EmailError("Microsoft a refusé le jeton : reconnectez le compte dans Réglages → E-mail.")
    if r.status_code == 403:
        raise EmailError("Autorisation « Envoyer des e-mails » manquante : cliquez sur « Reconnecter » et acceptez-la. "
                         f"({code or detail})")
    if r.status_code == 429:
        raise EmailError("Microsoft limite temporairement les envois : réessayez dans quelques minutes.")
    raise EmailError(f"Microsoft Graph a refusé l'envoi ({r.status_code} {code}) : {detail}")


def _send_microsoft(org: Organization, msg: EmailMessage) -> None:
    """Outlook.com / Microsoft 365 : via l'API Graph (par défaut) ou en SMTP + XOAUTH2 (port 587)."""
    account, host = settings.get(org, "ms_account"), microsoft.smtp_host(org)
    if not account:
        raise EmailError("Compte Microsoft non connecté (Réglages → E-mail → Se connecter avec Microsoft).")
    try:
        token = microsoft.access_token(org)
    except microsoft.MicrosoftError as exc:
        raise EmailError(str(exc)) from exc
    if microsoft.method(org) == "graph":
        return _send_graph(org, msg, token)
    # Au premier appel smtplib attend la réponse initiale ; si Microsoft répond par une erreur (334 + JSON),
    # il faut renvoyer une ligne vide pour obtenir le code d'échec définitif (535).
    auth = lambda challenge=None: microsoft.xoauth2(account, token) if challenge is None else ""  # noqa: E731
    try:
        with smtplib.SMTP(host, 587, timeout=20) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()  # obligatoire après STARTTLS ; sinon le serveur répond 503 et smtplib ne lève pas d'erreur
            code, _ = server.auth("XOAUTH2", auth)
            if code != 235:
                raise smtplib.SMTPAuthenticationError(code, b"authentication not accepted")
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        if "disabled" in detail.lower():
            raise EmailError("Microsoft a bloqué l'envoi SMTP pour cette boîte (« SmtpClientAuthentication disabled »). "
                             "Choisissez la méthode « API Microsoft Graph » dans Réglages → E-mail, enregistrez, puis "
                             "cliquez sur « Reconnecter ».") from exc
        raise EmailError(f"Microsoft a refusé l'identification ({detail[:160]}). Reconnectez le compte dans les réglages.") from exc
    except smtplib.SMTPSenderRefused as exc:
        raise EmailError(f"Microsoft refuse d'envoyer au nom de {msg['From']} : l'expéditeur doit être {account}.") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise EmailError(f"Adresse refusée par le serveur : {msg['To']}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Envoi impossible via {host} — {exc}") from exc

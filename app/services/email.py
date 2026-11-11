"""Envoi d'e-mails. Fournisseurs : off (désactivé), console (journal, pour tester) ou SMTP.

SMTP couvre presque tous les services : Gmail / Google Workspace, Outlook / Microsoft 365, OVH, Brevo,
Mailjet, Scaleway… L'envoi passe par la bibliothèque standard (smtplib), sans dépendance supplémentaire.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from ..models import Organization
from . import media, settings

log = logging.getLogger("sharedfleet.email")

SECURITY = {"starttls": "STARTTLS (port 587, le plus courant)", "ssl": "SSL/TLS (port 465)", "none": "Aucune (port 25, réseau local)"}


class EmailError(Exception):
    pass


def enabled(org: Organization) -> bool:
    return settings.get(org, "email_provider") in ("console", "smtp")


def looks_valid(address: str) -> bool:
    address = (address or "").strip()
    return "@" in address and "." in address.rsplit("@", 1)[-1] and " " not in address and "\n" not in address


def build(org: Organization, to: str, to_name: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    """Message multipart (texte + HTML). Le logo de la société est joint en image intégrée (cid:logo)."""
    sender = settings.get(org, "email_from")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.get(org, "email_from_name") or org.name, sender))
    msg["To"] = formataddr((to_name, to.strip())) if to_name else to.strip()
    reply_to = settings.get(org, "company_email")
    if looks_valid(reply_to) and reply_to != sender:
        msg["Reply-To"] = reply_to
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] if "@" in sender else None)
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

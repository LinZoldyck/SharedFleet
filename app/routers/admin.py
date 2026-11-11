"""Réglages de la société, intégrations et gestion des utilisateurs (réservé aux administrateurs)."""
import secrets

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import ROLES, User
from ..security import admin, hash_password, token_hash
from ..services import email, fines, media, settings, sms
from ..web import flash, owned, redirect, render, to_float

router = APIRouter()

# Champs enregistrés par section du formulaire de réglages
SECTIONS = {
    "societe": ("company_name", "company_address", "company_postal_code", "company_city", "company_siret",
                "company_vat", "company_phone", "company_email", "contract_terms"),
    "getaround": ("getaround_token", "getaround_webhook_secret"),
    "assistant": ("anthropic_api_key", "assistant_mode", "assistant_instructions"),
    "email": ("email_provider", "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password", "email_from",
              "email_from_name"),
    "sms": ("sms_provider", "twilio_sid", "twilio_token", "twilio_from", "sms_http_url", "sms_http_token"),
    "facturation": ("invoice_provider", "pennylane_token", "invoice_prefix"),
    "code": (),
}
CHECKBOXES = {"code": ("code_by_sms", "code_by_email"), "getaround": ("getaround_block_calendar", "getaround_include_insurance"), "facturation": ("invoice_draft",)}


@router.get("/reglages")
def settings_page(request: Request, user: User = Depends(admin), db: Session = Depends(get_db)):
    org = user.org
    values = {k: settings.get(org, k) for k in settings.DEFAULTS}
    secrets_set = {k: settings.has(org, k) for k in settings.SECRET_KEYS}
    users = db.query(User).filter_by(org_id=org.id).order_by(User.id).all()
    return render(request, "settings.html", s=values, smtp_security=email.SECURITY, secret=secrets_set, users=users, org=org,
                  webhook_url=f"{config.BASE_URL}/webhooks/getaround/{org.webhook_key}",
                  fines_inbox=fines.inbox(org.id), new_token=request.session.pop("new_token", None))


@router.post("/reglages/{section}")
async def save_settings(section: str, request: Request, user: User = Depends(admin), db: Session = Depends(get_db)):
    if section not in SECTIONS:
        return redirect("/reglages")
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in SECTIONS[section]}
    for box in CHECKBOXES.get(section, ()):
        values[box] = form.get(box) == "on"
    if section == "facturation":
        values["fine_admin_fee"] = to_float(form.get("fine_admin_fee"), 25)
    if section == "societe" and values["company_name"]:
        user.org.name = values["company_name"]
    settings.update(user.org, values)
    db.commit()
    flash(request, "Réglages enregistrés.")
    return redirect(f"/reglages#{section}")


@router.post("/reglages-logo")
async def upload_logo(request: Request, logo: UploadFile = File(), user: User = Depends(admin), db: Session = Depends(get_db)):
    try:
        path = media.save_image(user.org_id, "logo", await logo.read(media.MAX_UPLOAD + 1), max_side=600, keep_alpha=True)
        settings.update(user.org, {"logo_path": path})
        db.commit()
        flash(request, "Logo enregistré : il apparaît en en-tête des contrats et des factures.")
    except media.InvalidImage as exc:
        flash(request, str(exc), "error")
    return redirect("/reglages#societe")


@router.post("/reglages-sms-test")
def sms_test(request: Request, to: str = Form(), user: User = Depends(admin)):
    ok = sms.send_sms(user.org, to, f"{user.org.name} : SMS de test envoyé depuis SharedFleet.")
    flash(request, "SMS de test envoyé (en mode console, il est seulement écrit dans le journal)." if ok
          else "Échec de l'envoi : vérifiez les paramètres.", "ok" if ok else "error")
    return redirect("/reglages#sms")


@router.post("/reglages-email-test")
def email_test(request: Request, to: str = Form(), user: User = Depends(admin)):
    if not email.looks_valid(to):
        flash(request, "Adresse e-mail invalide.", "error")
        return redirect("/reglages#email")
    name = settings.get(user.org, "company_name") or user.org.name
    try:
        email.send(user.org, email.build(user.org, to, "", f"{name} : e-mail de test", "Cet e-mail de test confirme "
                   "que SharedFleet peut envoyer les codes d'accès à vos clients.",
                   "<p>{{logo}}</p><p>Cet e-mail de test confirme que SharedFleet peut envoyer les codes d'accès à vos clients.</p>"))
        flash(request, "E-mail de test envoyé (en mode test, il est seulement écrit dans le journal)."
              if settings.get(user.org, "email_provider") == "console" else f"E-mail de test envoyé à {to}.")
    except email.EmailError as exc:
        flash(request, str(exc), "error")
    return redirect("/reglages#email")


@router.post("/reglages-jeton")
def new_api_token(request: Request, user: User = Depends(admin), db: Session = Depends(get_db)):
    """Génère le jeton de l'extension Chrome. Il n'est affiché qu'une fois ; seule son empreinte est stockée."""
    token = "sf_" + secrets.token_urlsafe(32)
    user.api_token_hash = token_hash(token)
    db.commit()
    request.session["new_token"] = token
    return redirect("/reglages#extension")


@router.post("/utilisateurs")
def add_user(request: Request, name: str = Form(), email: str = Form(), role: str = Form(), password: str = Form(),
             user: User = Depends(admin), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if role not in ROLES or len(password) < 10 or "@" not in email:
        flash(request, "Vérifiez l'e-mail, le rôle et le mot de passe (10 caractères minimum).", "error")
    elif db.query(User).filter_by(email=email).first():
        flash(request, "Cet e-mail est déjà utilisé.", "error")
    else:
        db.add(User(org_id=user.org_id, name=name.strip(), email=email, role=role, password_hash=hash_password(password)))
        db.commit()
        flash(request, f"Compte créé pour {name}.")
    return redirect("/reglages#utilisateurs")


@router.post("/utilisateurs/{uid}")
def update_user(uid: int, request: Request, role: str = Form(), active: str = Form(""), password: str = Form(""),
                user: User = Depends(admin), db: Session = Depends(get_db)):
    target = owned(db, User, uid, user)
    if target.id == user.id and (role != "admin" or active != "on"):
        flash(request, "Vous ne pouvez pas retirer vos propres droits d'administrateur.", "error")
        return redirect("/reglages#utilisateurs")
    if role in ROLES:
        target.role = role
    target.active = active == "on"
    if password:
        if len(password) < 10:
            flash(request, "Mot de passe trop court (10 caractères minimum).", "error")
            return redirect("/reglages#utilisateurs")
        target.password_hash = hash_password(password)
    db.commit()
    flash(request, "Utilisateur mis à jour.")
    return redirect("/reglages#utilisateurs")

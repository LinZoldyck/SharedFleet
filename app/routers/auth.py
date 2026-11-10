import secrets

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Organization, User
from ..security import clear_attempts, client_ip, hash_password, too_many_attempts, verify_password
from ..services import settings
from ..web import flash, redirect, render

router = APIRouter()


def signup_open(db: Session) -> bool:
    """L'inscription est toujours possible tant qu'aucun compte n'existe (première installation)."""
    return config.ALLOW_SIGNUP or db.query(User).count() == 0


@router.get("/connexion")
def login_page(request: Request, db: Session = Depends(get_db)):
    if db.query(User).count() == 0:
        return redirect("/inscription")
    return render(request, "login.html", signup=signup_open(db))


@router.post("/connexion")
def login(request: Request, email: str = Form(), password: str = Form(), db: Session = Depends(get_db)):
    key = f"login:{client_ip(request)}"
    if too_many_attempts(key):
        flash(request, "Trop de tentatives. Réessayez dans quelques minutes.", "error")
        return redirect("/connexion")
    user = db.query(User).filter_by(email=email.strip().lower()).first()
    if not user or not user.active or not verify_password(password, user.password_hash):
        flash(request, "E-mail ou mot de passe incorrect.", "error")
        return redirect("/connexion")
    clear_attempts(key)
    request.session.clear()
    request.session["uid"] = user.id
    return redirect("/")


@router.get("/inscription")
def signup_page(request: Request, db: Session = Depends(get_db)):
    if not signup_open(db):
        return redirect("/connexion")
    return render(request, "signup.html")


@router.post("/inscription")
def signup(request: Request, company: str = Form(), name: str = Form(), email: str = Form(),
           password: str = Form(), db: Session = Depends(get_db)):
    if not signup_open(db):
        return redirect("/connexion")
    email = email.strip().lower()
    if len(password) < 10:
        flash(request, "Le mot de passe doit contenir au moins 10 caractères.", "error")
        return redirect("/inscription")
    if "@" not in email or db.query(User).filter_by(email=email).first():
        flash(request, "Cet e-mail est invalide ou déjà utilisé.", "error")
        return redirect("/inscription")
    org = Organization(name=company.strip(), webhook_key=secrets.token_urlsafe(24), settings={})
    settings.update(org, {"company_name": company.strip(), "company_email": email})
    db.add(org)
    db.flush()
    user = User(org_id=org.id, email=email, name=name.strip(), password_hash=hash_password(password), role="admin")
    db.add(user)
    db.commit()
    request.session.clear()
    request.session["uid"] = user.id
    flash(request, "Bienvenue ! Commencez par ajouter vos véhicules, puis complétez les réglages de votre société.")
    return redirect("/vehicules")


@router.post("/deconnexion")
def logout(request: Request):
    request.session.clear()
    return redirect("/connexion")

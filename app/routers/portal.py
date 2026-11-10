"""Parcours du client (location hors plateforme), pensé pour le téléphone :
code reçu par SMS → code de la boîte à clés → état des lieux de départ → … → état des lieux de retour."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Booking, Organization, Photo
from ..security import clear_attempts, client_ip, too_many_attempts
from ..services import bookings as svc
from ..services import media, pdf
from ..timeutil import now
from ..web import flash, read_uploads, redirect, render, to_int

router = APIRouter(prefix="/acces")


def portal_booking(request: Request, db: Session = Depends(get_db)) -> Booking:
    """Location ouverte par le code, revalidée à chaque requête (le code expire avec la location)."""
    code = request.session.get("portal_code")
    booking = svc.find_by_code(db, code) if code else None
    if not booking and code:
        # Après l'état des lieux de retour, on laisse le client voir l'écran final pendant sa session
        done = db.query(Booking).filter_by(access_code=code, status="completed").first()
        if done and request.session.get("portal_done") == done.id:
            return done
    if not booking:
        request.session.pop("portal_code", None)
        raise HTTPException(303, headers={"Location": "/acces"})
    return booking


@router.get("")
def code_page(request: Request):
    if request.session.get("portal_code"):
        return redirect("/acces/location")
    return render(request, "portal/code.html")


@router.post("")
def enter_code(request: Request, code: str = Form(), db: Session = Depends(get_db)):
    key = f"portal:{client_ip(request)}"
    if too_many_attempts(key, limit=6, window=900):
        flash(request, "Trop d'essais. Patientez 15 minutes ou contactez le loueur.", "error")
        return redirect("/acces")
    booking = svc.find_by_code(db, code)
    if not booking:
        flash(request, "Code inconnu ou pas encore actif. Il fonctionne à partir de 30 minutes avant le départ.", "error")
        return redirect("/acces")
    clear_attempts(key)
    request.session["portal_code"] = booking.access_code
    return redirect("/acces/location")


@router.get("/location")
def booking_home(request: Request, b: Booking = Depends(portal_booking), db: Session = Depends(get_db)):
    return render(request, "portal/home.html", b=b, org=db.get(Organization, b.org_id))


@router.get("/etat-des-lieux/{kind}")
def inspection_form(kind: str, request: Request, b: Booking = Depends(portal_booking), db: Session = Depends(get_db)):
    expected = "checkin" if not b.inspection("checkin") else "checkout"
    if kind != expected or b.status == "completed":
        return redirect("/acces/location")
    return render(request, "portal/inspection.html", b=b, kind=kind, org=db.get(Organization, b.org_id))


@router.post("/etat-des-lieux/{kind}")
async def save_inspection(kind: str, request: Request, mileage: str = Form(), fuel_level: str = Form(),
                          notes: str = Form(""), signed_name: str = Form(""), accept: str = Form(""),
                          exterior: list[UploadFile] = File(default=[]), interior: list[UploadFile] = File(default=[]),
                          b: Booking = Depends(portal_booking), db: Session = Depends(get_db)):
    expected = "checkin" if not b.inspection("checkin") else "checkout"
    if kind != expected or b.status == "completed":
        return redirect("/acces/location")
    ext, inte = await read_uploads(exterior), await read_uploads(interior)
    km = to_int(mileage)
    problem = None
    if km is None or km < 0:
        problem = "Indiquez le kilométrage affiché au compteur."
    elif len(ext) < 4 or len(inte) < 1:
        problem = "Ajoutez au moins 4 photos de l'extérieur (une par côté) et 1 photo de l'intérieur."
    elif kind == "checkin" and (accept != "on" or len(signed_name.strip()) < 3):
        problem = "Saisissez votre nom et cochez la case pour accepter le contrat."
    elif kind == "checkout" and b.inspection("checkin").mileage is not None and km < b.inspection("checkin").mileage:
        problem = "Le kilométrage de retour ne peut pas être inférieur à celui du départ."
    if problem:
        flash(request, problem, "error")
        return redirect(f"/acces/etat-des-lieux/{kind}")
    try:
        if kind == "checkin":
            b.accepted_at, b.accepted_by, b.accepted_ip = now(), signed_name.strip()[:200], client_ip(request)
        svc.save_inspection(db, b, kind, km, to_int(fuel_level), notes, ext, inte)
    except media.InvalidImage as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/acces/etat-des-lieux/{kind}")
    if kind == "checkout":
        request.session["portal_done"] = b.id
    return redirect("/acces/location")


@router.get("/contrat.pdf")
def contract(b: Booking = Depends(portal_booking), db: Session = Depends(get_db)):
    return Response(pdf.contract_pdf(db.get(Organization, b.org_id), b), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="contrat-{b.reference}.pdf"'})


@router.get("/photo/{pid}")
def photo(pid: int, b: Booking = Depends(portal_booking), db: Session = Depends(get_db)):
    ph = db.get(Photo, pid)
    path = media.absolute(ph.path) if ph and ph.inspection_id in [i.id for i in b.inspections] else None
    if not path:
        raise HTTPException(404, "Introuvable")
    return FileResponse(path)


@router.post("/quitter")
def leave(request: Request):
    request.session.pop("portal_code", None)
    request.session.pop("portal_done", None)
    return redirect("/acces")

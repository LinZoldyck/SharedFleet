"""Contraventions : dépôt, rapprochement, désignation (API de l'extension Chrome), refacturation."""
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Booking, Fine, User
from ..security import api_user, current_user, manager
from ..services import fines as svc
from ..services import invoicing, media, settings
from ..timeutil import now
from ..web import flash, owned, redirect, render, to_datetime, to_float

router = APIRouter()
OPEN = ("to_review", "unmatched", "matched")


@router.get("/contraventions")
def list_fines(request: Request, f: str = "ouvertes", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = db.query(Fine).filter_by(org_id=user.org_id)
    if f == "ouvertes":
        q = q.filter(Fine.status.in_(OPEN))
    return render(request, "fines.html", fines=q.order_by(Fine.created_at.desc()).limit(200).all(), f=f,
                  inbox=svc.inbox(user.org_id), ai_ready=settings.has(user.org, "anthropic_api_key"))


@router.post("/contraventions")
async def upload(request: Request, files: list[UploadFile] = File(), user: User = Depends(manager),
                 db: Session = Depends(get_db)):
    """Dépôt depuis le navigateur : les fichiers passent par le même dossier que le dépôt réseau."""
    saved = 0
    for f in files:
        suffix = "." + f.filename.rsplit(".", 1)[-1].lower() if f.filename and "." in f.filename else ""
        if suffix not in svc.MEDIA_TYPES:
            flash(request, f"{f.filename} ignoré : formats acceptés PDF, JPG, PNG.", "error")
            continue
        data = await f.read(media.MAX_UPLOAD + 1)
        if len(data) > media.MAX_UPLOAD:
            flash(request, f"{f.filename} ignoré : fichier trop volumineux.", "error")
            continue
        (svc.inbox(user.org_id) / f"depot_{now():%Y%m%d%H%M%S}_{saved}{suffix}").write_bytes(data)
        saved += 1
    count = svc.scan_inbox(db, user.org)
    flash(request, f"{count} avis lu(s)." + (" Les doublons ont été ignorés." if count < saved else ""))
    return redirect("/contraventions")


@router.get("/contraventions/{fid}")
def detail(fid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    fine = owned(db, Fine, fid, user)
    candidates = []
    if fine.vehicle_id:
        candidates = db.query(Booking).filter(Booking.org_id == user.org_id, Booking.vehicle_id == fine.vehicle_id,
                                              Booking.status != "cancelled").order_by(Booking.starts_at.desc()).limit(30).all()
    return render(request, "fine_detail.html", fine=fine, candidates=candidates, d=svc.designation(user.org, fine),
                  fee=settings.get(user.org, "fine_admin_fee"))


@router.get("/contraventions/{fid}/fichier")
def original(fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    fine = owned(db, Fine, fid, user)
    path = media.absolute(fine.file_path)
    return FileResponse(path) if path else redirect(f"/contraventions/{fid}")


@router.post("/contraventions/{fid}")
def correct(fid: int, request: Request, kind: str = Form(), notice_number: str = Form(""), plate: str = Form(""),
            offense_at: str = Form(""), offense_type: str = Form(""), location: str = Form(""), amount: str = Form(""),
            booking_id: str = Form(""), user: User = Depends(manager), db: Session = Depends(get_db)):
    """Correction manuelle des champs lus, puis nouveau rapprochement (ou choix forcé de la location)."""
    fine = owned(db, Fine, fid, user)
    fine.kind = kind if kind in ("amende", "fps") else "amende"
    fine.notice_number, fine.plate = notice_number.strip(), plate.strip().upper()
    fine.offense_at, fine.offense_type, fine.location = to_datetime(offense_at), offense_type.strip(), location.strip()
    fine.amount = to_float(amount) if amount.strip() else None
    done = fine.status == "done"
    if booking_id.isdigit():
        b = owned(db, Booking, int(booking_id), user)
        fine.booking_id, fine.vehicle_id, fine.status = b.id, b.vehicle_id, "matched"
    else:
        svc.match(db, fine)
    if done:
        fine.status = "done"
    db.commit()
    flash(request, "Contravention mise à jour.")
    return redirect(f"/contraventions/{fid}")


@router.post("/contraventions/{fid}/statut")
def set_status(fid: int, request: Request, done: str = Form("1"), user: User = Depends(manager), db: Session = Depends(get_db)):
    fine = owned(db, Fine, fid, user)
    if done == "1":
        fine.status, fine.done_at = "done", now()
    else:
        fine.done_at = None
        svc.match(db, fine)
    db.commit()
    return redirect(f"/contraventions/{fid}")


@router.post("/contraventions/{fid}/refacturer")
def reinvoice(fid: int, request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    fine = owned(db, Fine, fid, user)
    if not fine.booking or not fine.booking.customer or not fine.amount:
        flash(request, "Il faut une location rapprochée, un client et un montant pour refacturer.", "error")
        return redirect(f"/contraventions/{fid}")
    try:
        inv = invoicing.create_invoice(db, user.org, fine.booking.customer, invoicing.fine_lines(user.org, fine),
                                       f"Refacturation avis n° {fine.notice_number}", booking=fine.booking)
        fine.status, fine.done_at = "done", now()
        db.commit()
        flash(request, f"Facture {inv.number or '(brouillon)'} créée et contravention marquée comme traitée.")
    except invoicing.InvoicingError as exc:
        flash(request, str(exc), "error")
    return redirect(f"/contraventions/{fid}")


# --- API utilisée par l'extension Chrome (jeton Bearer) ---
@router.get("/api/ext/fines")
def ext_fines(user: User = Depends(api_user), db: Session = Depends(get_db)):
    """Contraventions rapprochées d'une location et pas encore traitées, avec les données du conducteur."""
    items = db.query(Fine).filter_by(org_id=user.org_id, status="matched").order_by(Fine.offense_at.desc()).all()
    return {"org": user.org.name, "fines": [svc.designation(user.org, f) for f in items],
            "detail_url": f"{config.BASE_URL}/contraventions/"}


@router.post("/api/ext/fines/{fid}/done")
def ext_done(fid: int, user: User = Depends(api_user), db: Session = Depends(get_db)):
    fine = owned(db, Fine, fid, user)
    fine.status, fine.done_at = "done", now()
    db.commit()
    return {"status": "done"}

"""Locations : planning, création d'une location directe, contrat PDF, code d'accès, états des lieux."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Booking, Customer, Organization, User, Vehicle
from ..security import current_user, manager
from ..services import bookings as svc
from ..services import email, getaround, invoicing, media, pdf
from ..timeutil import now
from ..web import flash, owned, read_uploads, redirect, render, to_datetime, to_float, to_int

router = APIRouter()
PLANNING_DAYS = 14


def _planning(db: Session, org_id: int, vehicles: list[Vehicle]) -> dict:
    """Bandeau de disponibilité sur 14 jours : position (en %) de chaque location sur la ligne du véhicule."""
    start = datetime.combine(now().date(), datetime.min.time())
    end = start + timedelta(days=PLANNING_DAYS)
    total = (end - start).total_seconds()
    rows = []
    for v in vehicles:
        found = svc.conflicts(db, org_id, v.id, start, end)
        bars = [{"b": b, "left": 100 * max(0, (b.starts_at - start).total_seconds()) / total,
                 "width": 100 * ((min(b.ends_at, end) - max(b.starts_at, start)).total_seconds()) / total}
                for b in sorted(found, key=lambda b: b.starts_at)]
        rows.append({"vehicle": v, "bars": bars})
    return {"rows": rows, "days": [start + timedelta(days=i) for i in range(PLANNING_DAYS)],
            "now": 100 * (now() - start).total_seconds() / total}


@router.get("/locations")
def list_bookings(request: Request, f: str = "actives", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = db.query(Booking).filter_by(org_id=user.org_id)
    if f == "actives":
        q = q.filter(Booking.status.in_(svc.ACTIVE)).order_by(Booking.starts_at)
    elif f in ("direct", "getaround"):
        q = q.filter_by(source=f).order_by(Booking.starts_at.desc())
    else:
        q = q.order_by(Booking.starts_at.desc())
    vehicles = db.query(Vehicle).filter_by(org_id=user.org_id, active=True).order_by(Vehicle.name).all()
    return render(request, "bookings.html", bookings=q.limit(200).all(), f=f, planning=_planning(db, user.org_id, vehicles))


@router.get("/locations/nouvelle")
def new_booking(request: Request, customer_id: int = 0, user: User = Depends(manager), db: Session = Depends(get_db)):
    vehicles = db.query(Vehicle).filter_by(org_id=user.org_id, active=True).order_by(Vehicle.name).all()
    customers = db.query(Customer).filter_by(org_id=user.org_id, getaround_user_id=None).order_by(Customer.company_name, Customer.last_name).all()
    start = (now() + timedelta(days=1)).replace(hour=8, minute=0, second=0)
    return render(request, "booking_form.html", vehicles=vehicles, customers=customers, customer_id=customer_id,
                  start=start, end=start.replace(hour=18))


@router.post("/locations")
def create_booking(request: Request, vehicle_id: int = Form(), customer_id: int = Form(), starts_at: str = Form(),
                   ends_at: str = Form(), price_ht: str = Form(""), vat_rate: str = Form("20"), notes: str = Form(""),
                   user: User = Depends(manager), db: Session = Depends(get_db)):
    vehicle, customer = owned(db, Vehicle, vehicle_id, user), owned(db, Customer, customer_id, user)
    start, end = to_datetime(starts_at), to_datetime(ends_at)
    if not start or not end or end <= start:
        flash(request, "Les dates sont invalides : le retour doit être après le départ.", "error")
        return redirect("/locations/nouvelle")
    clash = svc.conflicts(db, user.org_id, vehicle.id, start, end)
    if clash:
        c = clash[0]
        flash(request, f"{vehicle.name} est déjà loué sur ce créneau ({c.reference}, du {c.starts_at:%d/%m %Hh%M} "
                       f"au {c.ends_at:%d/%m %Hh%M}).", "error")
        return redirect("/locations/nouvelle")
    b = Booking(org_id=user.org_id, vehicle_id=vehicle.id, customer_id=customer.id, source="direct", starts_at=start,
                ends_at=end, vat_rate=to_float(vat_rate, 20), notes=notes.strip(), deposit=vehicle.deposit)
    b.vehicle = vehicle
    b.price_ht = to_float(price_ht) if price_ht.strip() else round(vehicle.daily_rate * b.days, 2)
    b.km_included = vehicle.km_included * b.days
    b.access_code = svc.new_access_code(db)
    db.add(b)
    db.commit()
    flash(request, f"Location {b.reference} créée. Le contrat est prêt ; le code d'accès sera envoyé au client 30 minutes avant le départ.")
    error = getaround.block_calendar(user.org, b)
    if error:
        flash(request, f"Le calendrier Getaround n'a pas pu être bloqué : {error} Bloquez le créneau à la main.", "error")
    return redirect(f"/locations/{b.id}")


@router.get("/locations/{bid}")
def booking_detail(bid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = owned(db, Booking, bid, user)
    return render(request, "booking_detail.html", b=b, lines=invoicing.booking_lines(b),
                  provider=invoicing.PROVIDER_LABELS, code_history=svc.code_history(db, b),
                  email_on=email.enabled(user.org), channels=CHANNEL_LABELS)


@router.get("/locations/{bid}/contrat.pdf")
def contract(bid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = owned(db, Booking, bid, user)
    return Response(pdf.contract_pdf(user.org, b), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="contrat-{b.reference}.pdf"'})


CHANNEL_LABELS = {"sms": "SMS", "email": "e-mail"}


@router.post("/locations/{bid}/code")
def send_code(bid: int, request: Request, channel: str = Form("auto"), user: User = Depends(manager),
              db: Session = Depends(get_db)):
    b = owned(db, Booking, bid, user)
    results = svc.send_access_code(db, user.org, b, channels=(channel,) if channel in CHANNEL_LABELS else None)
    if not results:
        flash(request, "Aucun envoi possible : ajoutez un téléphone ou un e-mail au client, et activez un canal dans "
                       "Réglages → Envoi du code d'accès.", "error")
    for ch, outcome in results.items():
        target = b.customer.phone if ch == "sms" else b.customer.email
        if outcome == "ok":
            flash(request, f"Code d'accès envoyé par {CHANNEL_LABELS[ch]} ({target}).")
        else:
            flash(request, f"{CHANNEL_LABELS[ch].capitalize()} non envoyé à {target} : {outcome}", "error")
    return redirect(f"/locations/{bid}")


@router.post("/locations/{bid}/annuler")
def cancel(bid: int, request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    b = owned(db, Booking, bid, user)
    if b.source != "direct":
        flash(request, "Une location Getaround s'annule depuis Getaround.", "error")
        return redirect(f"/locations/{bid}")
    b.status = "cancelled"
    db.commit()
    error = getaround.block_calendar(user.org, b, release=True)
    flash(request, "Location annulée." + (f" Pensez à libérer le créneau sur Getaround ({error})." if error else ""))
    return redirect("/locations")


@router.post("/locations/{bid}/etat-des-lieux/{kind}")
async def staff_inspection(bid: int, kind: str, request: Request, mileage: str = Form(""), fuel_level: str = Form(""),
                           notes: str = Form(""), exterior: list[UploadFile] = File(default=[]),
                           interior: list[UploadFile] = File(default=[]), user: User = Depends(manager),
                           db: Session = Depends(get_db)):
    """État des lieux saisi par le loueur (remise des clés en main propre, ou correction)."""
    b = owned(db, Booking, bid, user)
    if kind not in ("checkin", "checkout"):
        return redirect(f"/locations/{bid}")
    try:
        svc.save_inspection(db, b, kind, to_int(mileage), to_int(fuel_level), notes,
                            await read_uploads(exterior), await read_uploads(interior))
        flash(request, "État des lieux enregistré.")
    except media.InvalidImage as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(f"/locations/{bid}")


@router.post("/locations/{bid}/facturer")
def invoice_booking(bid: int, request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    b = owned(db, Booking, bid, user)
    if b.source != "direct":
        flash(request, "Les locations Getaround sont facturées par Getaround.", "error")
        return redirect(f"/locations/{bid}")
    try:
        inv = invoicing.create_invoice(db, user.org, b.customer, invoicing.booking_lines(b),
                                       f"Location {b.vehicle.name} — {b.reference}", booking=b)
        flash(request, f"Facture {inv.number or '(brouillon)'} créée.")
    except invoicing.InvoicingError as exc:
        flash(request, str(exc), "error")
    return redirect(f"/locations/{bid}")

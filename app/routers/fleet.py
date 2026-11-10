"""Véhicules, clients et coûts."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Booking, Customer, Expense, User, Vehicle
from ..security import current_user, manager
from ..timeutil import now
from ..web import flash, owned, redirect, render, to_date, to_float, to_int

router = APIRouter()
EXPENSE_CATEGORIES = ["Carburant", "Entretien", "Réparation", "Assurance", "Pneus", "Nettoyage", "Péage / parking",
                      "Commission plateforme", "Autre"]


async def _fill(obj, request: Request, text: tuple, floats: tuple = (), ints: tuple = (), dates: tuple = ()):
    form = await request.form()
    for f in text:
        setattr(obj, f, str(form.get(f, "")).strip())
    for f in floats:
        setattr(obj, f, to_float(form.get(f)))
    for f in ints:
        setattr(obj, f, to_int(form.get(f)) or 0)
    for f in dates:
        setattr(obj, f, to_date(form.get(f)))
    return form


# --- véhicules ---
@router.get("/vehicules")
def vehicles(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.query(Vehicle).filter_by(org_id=user.org_id).order_by(Vehicle.active.desc(), Vehicle.name).all()
    return render(request, "vehicles.html", vehicles=items)


@router.get("/vehicules/nouveau")
def vehicle_new(request: Request, user: User = Depends(manager)):
    return render(request, "vehicle_form.html", v=Vehicle(km_included=200, extra_km_price=0.30, deposit=1000, active=True))


@router.get("/vehicules/{vid}")
def vehicle_edit(vid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "vehicle_form.html", v=owned(db, Vehicle, vid, user))


@router.post("/vehicules/{vid}")
async def vehicle_save(vid: int, request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    v = owned(db, Vehicle, vid, user) if vid else Vehicle(org_id=user.org_id)
    form = await _fill(v, request, ("name", "plate", "brand", "model", "address", "lock_code"),
                       floats=("daily_rate", "extra_km_price", "deposit", "monthly_cost"), ints=("km_included",))
    v.plate = v.plate.upper()
    v.active = form.get("active") == "on"
    if not v.name or not v.plate:
        flash(request, "Le nom et l'immatriculation sont obligatoires.", "error")
        return redirect(f"/vehicules/{vid}" if vid else "/vehicules/nouveau")
    db.add(v)
    db.commit()
    flash(request, "Véhicule enregistré.")
    return redirect("/vehicules")


# --- clients ---
@router.get("/clients")
def customers(request: Request, q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = db.query(Customer).filter_by(org_id=user.org_id)
    if q:
        like = f"%{q}%"
        query = query.filter(Customer.company_name.ilike(like) | Customer.last_name.ilike(like) |
                             Customer.first_name.ilike(like) | Customer.email.ilike(like))
    return render(request, "customers.html", customers=query.order_by(Customer.id.desc()).limit(200).all(), q=q)


@router.get("/clients/nouveau")
def customer_new(request: Request, user: User = Depends(manager)):
    return render(request, "customer_form.html", c=Customer(country="France", license_country="France"), bookings=[])


@router.get("/clients/{cid}")
def customer_edit(cid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = owned(db, Customer, cid, user)
    history = db.query(Booking).filter_by(customer_id=c.id).order_by(Booking.starts_at.desc()).limit(20).all()
    return render(request, "customer_form.html", c=c, bookings=history)


@router.post("/clients/{cid}")
async def customer_save(cid: int, request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    c = owned(db, Customer, cid, user) if cid else Customer(org_id=user.org_id)
    form = await _fill(c, request, ("company_name", "siret", "vat_number", "first_name", "last_name", "email", "phone",
                                    "address", "postal_code", "city", "country", "license_number", "license_country"),
                       dates=("birth_date", "license_date"))
    if not (c.company_name or c.last_name):
        flash(request, "Indiquez une raison sociale ou un nom.", "error")
        return redirect(f"/clients/{cid}" if cid else "/clients/nouveau")
    db.add(c)
    db.commit()
    flash(request, "Client enregistré.")
    # Création depuis le formulaire de location : on y retourne avec le client sélectionné
    return redirect(f"/locations/nouvelle?customer_id={c.id}" if form.get("back") == "booking" else "/clients")


# --- coûts ---
@router.get("/couts")
def expenses(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.query(Expense).filter_by(org_id=user.org_id).order_by(Expense.spent_on.desc()).limit(300).all()
    vehicles_ = db.query(Vehicle).filter_by(org_id=user.org_id).order_by(Vehicle.name).all()
    return render(request, "expenses.html", expenses=items, vehicles=vehicles_, categories=EXPENSE_CATEGORIES,
                  today=now().date())


@router.post("/couts")
def expense_add(request: Request, spent_on: str = Form(), category: str = Form(), amount: str = Form(),
                label: str = Form(""), vehicle_id: str = Form(""), user: User = Depends(manager),
                db: Session = Depends(get_db)):
    value = to_float(amount)
    if value <= 0 or not to_date(spent_on):
        flash(request, "Indiquez une date et un montant positif.", "error")
        return redirect("/couts")
    vehicle = owned(db, Vehicle, int(vehicle_id), user) if vehicle_id.isdigit() else None
    db.add(Expense(org_id=user.org_id, spent_on=to_date(spent_on), category=category[:40], label=label.strip()[:200],
                   amount=value, vehicle_id=vehicle.id if vehicle else None))
    db.commit()
    flash(request, "Coût ajouté.")
    return redirect("/couts")


@router.post("/couts/{eid}/supprimer")
def expense_delete(eid: int, request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    db.delete(owned(db, Expense, eid, user))
    db.commit()
    flash(request, "Coût supprimé.")
    return redirect("/couts")

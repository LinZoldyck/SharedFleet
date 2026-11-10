import math

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, Vehicle
from ..security import current_user
from ..services import dashboard, media
from ..web import redirect, render

router = APIRouter()


@router.get("/")
def home(request: Request, p: str = "mois", user: User = Depends(current_user), db: Session = Depends(get_db)):
    if p not in dashboard.PERIODS:
        p = "mois"
    if not db.query(Vehicle).filter_by(org_id=user.org_id).count():
        return redirect("/vehicules")
    data = dashboard.compute(db, user.org_id, p)
    top = max([m["revenue"] for m in data["months"]] + [1])
    low = min([m["margin"] for m in data["months"]] + [0])
    high = max([m["margin"] for m in data["months"]] + [1])
    step = dashboard.nice_scale(max(high, -low))[1]
    margin_ticks = [step * i for i in range(-math.ceil(-low / step), math.ceil(high / step) + 1)]
    return render(request, "dashboard.html", d=data, periods=dashboard.PERIODS,
                  rev_ticks=dashboard.nice_scale(top), margin_ticks=margin_ticks)


@router.get("/media/{path:path}")
def stored_file(path: str, user: User = Depends(current_user)):
    """Sert un fichier stocké (photo, logo, avis) — uniquement ceux de la société de l'utilisateur."""
    file = media.absolute(path) if path.startswith(f"org_{user.org_id}/") else None
    if not file:
        return render_404()
    return FileResponse(file)


def render_404():
    from fastapi import HTTPException
    raise HTTPException(404, "Introuvable")

"""Outils communs aux pages : gabarits, messages flash, filtres d'affichage, lecture des formulaires."""
from datetime import date, datetime
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config
from .models import ROLES
from .security import ROLE_LEVEL
from .services import media
from .timeutil import now

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUS = {  # libellé, couleur de pastille
    "confirmed": ("À venir", "blue"), "in_progress": ("En cours", "green"), "completed": ("Terminée", "gray"),
    "cancelled": ("Annulée", "red"),
    "to_review": ("À vérifier", "amber"), "unmatched": ("Sans location", "red"), "matched": ("Location trouvée", "blue"),
    "done": ("Traitée", "green"),
    "new": ("Nouveau", "blue"), "draft": ("Brouillon proposé", "amber"), "escalated": ("À traiter par vous", "red"),
    "auto_replied": ("Réponse automatique", "green"), "handled": ("Traité", "gray"), "sent": ("Envoyé", "gray"),
    "final": ("Finalisée", "green"),
}
DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def eur(value, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{decimals}f} €".replace(",", " ").replace(".", ",")


def fdt(value: datetime | None) -> str:
    return f"{DAYS[value.weekday()]} {value:%d/%m/%Y} · {value:%Hh%M}" if value else "—"


def fdate(value) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


templates.env.filters.update(eur=eur, dt=fdt, date=fdate)
templates.env.globals.update(STATUS=STATUS, ROLES=ROLES, ROLE_LEVEL=ROLE_LEVEL, now=now, config=config)


def flash(request: Request, text: str, kind: str = "ok") -> None:
    # Nouvelle liste à chaque fois : une modification « en place » ne serait pas vue par le middleware de session
    request.session["flash"] = [*request.session.get("flash", []), [kind, text]][-5:]


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx["user"] = getattr(request.state, "user", None)
    ctx["flashes"] = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def owned(db, model, obj_id: int, user):
    """Charge un objet en vérifiant qu'il appartient à la société de l'utilisateur."""
    obj = db.get(model, obj_id)
    if not obj or obj.org_id != user.org_id:
        raise HTTPException(404, "Introuvable")
    return obj


# --- lecture tolérante des champs de formulaire ---
def to_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float((value or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        return default


def to_int(value: str | None) -> int | None:
    try:
        return int(float((value or "").replace(",", ".").replace(" ", "")))
    except ValueError:
        return None


def to_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def to_datetime(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value or "").replace(tzinfo=None)
    except ValueError:
        return None


async def read_uploads(files: list[UploadFile] | None, limit: int = 12) -> list[bytes]:
    """Contenu des fichiers envoyés (les champs vides sont ignorés)."""
    out = []
    for f in (files or [])[:limit]:
        if f and f.filename:
            data = await f.read(media.MAX_UPLOAD + 1)
            if data:
                out.append(data)
    return out

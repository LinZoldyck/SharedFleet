"""Import manuel des locations Getaround : coller un tableau JSON, prévisualiser, importer."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import manager
from ..services import getaround_import as imp
from ..services import settings
from ..web import render

router = APIRouter()
URL = "/locations/import-getaround"


def _page(request: Request, user: User, text: str = "", rows=None, error: str = "", done: bool = False):
    return render(request, "getaround_import.html", text=text, rows=rows, error=error, done=done, actions=imp.ACTIONS,
                  counts=imp.summary(rows) if rows else None, example=imp.EXAMPLE,
                  with_insurance=settings.get(user.org, "getaround_include_insurance"))


@router.get(URL)
def import_page(request: Request, user: User = Depends(manager)):
    return _page(request, user)


@router.post(URL)
def import_run(request: Request, payload: str = Form(""), action: str = Form("preview"), user: User = Depends(manager),
               db: Session = Depends(get_db)):
    try:
        rows = imp.plan(db, user.org, payload)
    except imp.ImportDataError as exc:
        return _page(request, user, payload, error=str(exc))
    if action == "import":
        imp.apply(db, user.org, rows)
    return _page(request, user, payload, rows=rows, done=action == "import")

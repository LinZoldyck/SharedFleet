"""Imports JSON : locations Getaround (relevées dans l'espace propriétaire) et locations directes déjà réalisées."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import manager
from ..services import direct_import
from ..services import getaround_import as imp
from ..services import settings
from ..web import render

router = APIRouter()
URL = "/locations/import-getaround"
DIRECT_URL = "/locations/import-directes"


def _page(request: Request, user: User, kind: str, text: str = "", rows=None, error: str = "", done: bool = False):
    module = imp if kind == "getaround" else direct_import
    return render(request, "getaround_import.html", kind=kind, url=URL if kind == "getaround" else DIRECT_URL,
                  text=text, rows=rows, error=error, done=done, actions=imp.ACTIONS,
                  counts=imp.summary(rows) if rows else None, example=module.EXAMPLE,
                  with_insurance=settings.get(user.org, "getaround_include_insurance"))


def _run(request: Request, user: User, db: Session, kind: str, payload: str, action: str):
    module = imp if kind == "getaround" else direct_import
    try:
        rows = module.plan(db, user.org, payload)
    except imp.ImportDataError as exc:
        return _page(request, user, kind, payload, error=str(exc))
    if action == "import":
        module.apply(db, user.org, rows)
    return _page(request, user, kind, payload, rows=rows, done=action == "import")


@router.get(URL)
def import_page(request: Request, user: User = Depends(manager)):
    return _page(request, user, "getaround")


@router.post(URL)
def import_run(request: Request, payload: str = Form(""), action: str = Form("preview"), user: User = Depends(manager),
               db: Session = Depends(get_db)):
    return _run(request, user, db, "getaround", payload, action)


@router.get(DIRECT_URL)
def direct_import_page(request: Request, user: User = Depends(manager)):
    return _page(request, user, "direct")


@router.post(DIRECT_URL)
def direct_import_run(request: Request, payload: str = Form(""), action: str = Form("preview"),
                      user: User = Depends(manager), db: Session = Depends(get_db)):
    return _run(request, user, db, "direct", payload, action)

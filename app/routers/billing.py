from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Invoice, User
from ..security import current_user
from ..services import invoicing, pdf
from ..web import owned, redirect, render

router = APIRouter()


@router.get("/factures")
def invoices(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.query(Invoice).filter_by(org_id=user.org_id).order_by(Invoice.id.desc()).limit(300).all()
    return render(request, "invoices.html", invoices=items, provider=invoicing.PROVIDER_LABELS)


@router.get("/factures/{iid}/pdf")
def invoice_file(iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    inv = owned(db, Invoice, iid, user)
    if inv.provider != "local":
        return redirect(inv.url if inv.url.startswith("https://") else "/factures")
    return Response(pdf.invoice_pdf(user.org, inv), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="facture-{inv.number}.pdf"'})

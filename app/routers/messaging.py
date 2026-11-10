"""Messagerie Getaround : boîte de réception, réponses, synchronisation manuelle et webhook."""
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Booking, Message, Organization, User
from ..security import current_user, manager
from ..services import assistant, getaround, settings
from ..web import flash, owned, redirect, render

router = APIRouter()
OPEN = ("new", "draft", "escalated")


@router.get("/messages")
def inbox(request: Request, f: str = "ouverts", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = db.query(Message).filter_by(org_id=user.org_id, direction="in")
    if f == "ouverts":
        q = q.filter(Message.status.in_(OPEN))
    items = q.order_by(Message.sent_at.desc()).limit(100).all()
    threads = {m.id: db.query(Message).filter_by(booking_id=m.booking_id).order_by(Message.sent_at).all() for m in items}
    return render(request, "messages.html", messages=items, threads=threads, f=f,
                  mode=settings.get(user.org, "assistant_mode"), connected=settings.has(user.org, "getaround_token"),
                  ai_ready=settings.has(user.org, "anthropic_api_key"))


@router.post("/messages/{mid}/repondre")
def reply(mid: int, request: Request, text: str = Form(), user: User = Depends(manager), db: Session = Depends(get_db)):
    msg = owned(db, Message, mid, user)
    api = getaround.client_for(user.org)
    if not api or not text.strip():
        flash(request, "Réponse vide ou Getaround non connecté.", "error")
        return redirect("/messages")
    try:
        assistant.send_reply(db, user.org, api, msg, text.strip(), by_ai=text.strip() == msg.ai_draft.strip())
        msg.status = "handled"
        db.commit()
        flash(request, "Réponse envoyée sur Getaround.")
    except getaround.GetaroundError as exc:
        flash(request, str(exc), "error")
    return redirect("/messages")


@router.post("/messages/{mid}/traite")
def mark_handled(mid: int, request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    owned(db, Message, mid, user).status = "handled"
    db.commit()
    return redirect("/messages")


@router.post("/getaround/synchroniser")
def sync_now(request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    try:
        s = getaround.sync_all(db, user.org)
        flash(request, f"Getaround synchronisé : {s['rentals']} location(s), {s['messages']} nouveau(x) message(s), "
                       f"{s['cars']} véhicule(s) rattaché(s).")
    except getaround.GetaroundError as exc:
        flash(request, str(exc), "error")
    return redirect(request.headers.get("referer") or "/locations")


@router.post("/webhooks/getaround/{key}")
async def webhook(key: str, request: Request, db: Session = Depends(get_db)):
    """Point d'entrée des événements Getaround. L'URL contient une clé propre à la société ; la signature
    HMAC (X-Drivy-Signature) est vérifiée avec le secret fourni par Getaround."""
    org = db.query(Organization).filter_by(webhook_key=key).first()
    if not org:
        raise HTTPException(404, "Inconnu")
    body = await request.body()
    if not getaround.verify_signature(settings.get(org, "getaround_webhook_secret"), body,
                                      request.headers.get("x-drivy-signature")):
        raise HTTPException(401, "Signature invalide")
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(400, "JSON invalide") from None
    try:
        return {"status": getaround.handle_webhook(db, org, event)}
    except getaround.GetaroundError:
        # Accusé de réception malgré tout : la synchronisation périodique rattrapera l'événement
        return {"status": "deferred"}

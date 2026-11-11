"""Tâches de fond : envoi des codes d'accès (SMS / e-mail), synchronisation Getaround, relève du dossier des contraventions."""
import asyncio
import logging
from datetime import timedelta

from ..db import SessionLocal
from ..models import Organization
from ..timeutil import now
from . import bookings, fines, getaround, settings

RETRY_MINUTES = 10  # délai avant de retenter l'envoi d'un code qui a échoué

log = logging.getLogger("sharedfleet.scheduler")


def send_due_codes() -> int:
    with SessionLocal() as db:
        sent = 0
        for b in bookings.due_for_code(db):
            last = bookings.code_history(db, b)
            if last and last[0]["at"] > now() - timedelta(minutes=RETRY_MINUTES):
                continue  # échec récent : on réessaie plus tard plutôt qu'à chaque minute
            bookings.send_access_code(db, db.get(Organization, b.org_id), b)
            sent += 1
        return sent


def sync_getaround() -> None:
    with SessionLocal() as db:
        for org in db.query(Organization):
            if settings.has(org, "getaround_token"):
                try:
                    getaround.sync_all(db, org)
                except getaround.GetaroundError as exc:
                    log.warning("Synchronisation Getaround (%s) : %s", org.name, exc)


def scan_fines() -> None:
    with SessionLocal() as db:
        for org in db.query(Organization):
            fines.scan_inbox(db, org)


JOBS = ((send_due_codes, 60), (scan_fines, 120), (sync_getaround, 300))  # (tâche, période en secondes)


async def run() -> None:
    tick = 0
    while True:
        for job, every in JOBS:
            if tick % every == 0:
                try:
                    await asyncio.to_thread(job)
                except Exception:  # noqa: BLE001 — une tâche en échec ne doit pas arrêter les autres
                    log.exception("Tâche de fond %s en échec", job.__name__)
        await asyncio.sleep(60)
        tick += 60

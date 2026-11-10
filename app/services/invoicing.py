"""Facturation. Un « fournisseur » par outil : Pennylane aujourd'hui, d'autres demain (version SaaS).

Pour ajouter un outil : créer une classe avec la méthode create(org, customer, lines, label) qui retourne
un dict {external_id, number, url, status}, puis l'enregistrer dans PROVIDERS.
"""
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..models import Booking, Customer, Fine, Invoice, Organization
from ..timeutil import now
from . import settings


class InvoicingError(Exception):
    pass


def totals(lines: list[dict]) -> tuple[float, float]:
    ht = sum(l["quantity"] * l["unit_price"] for l in lines)
    ttc = sum(l["quantity"] * l["unit_price"] * (1 + l["vat_rate"] / 100) for l in lines)
    return round(ht, 2), round(ttc, 2)


def booking_lines(b: Booking) -> list[dict]:
    lines = [{"label": f"Location {b.vehicle.brand} {b.vehicle.model} ({b.vehicle.plate}) du "
                       f"{b.starts_at:%d/%m/%Y %Hh%M} au {b.ends_at:%d/%m/%Y %Hh%M} — contrat {b.reference}",
              "quantity": 1, "unit_price": round(b.price_ht, 2), "vat_rate": b.vat_rate}]
    cin, cout = b.inspection("checkin"), b.inspection("checkout")
    if b.km_included and cin and cout and cin.mileage is not None and cout.mileage is not None:
        extra = cout.mileage - cin.mileage - b.km_included
        if extra > 0:
            lines.append({"label": f"Kilomètres supplémentaires ({b.km_included} km inclus)", "quantity": extra,
                          "unit_price": b.vehicle.extra_km_price, "vat_rate": b.vat_rate})
    return lines


def fine_lines(org: Organization, fine: Fine) -> list[dict]:
    """Refacturation d'un FPS : le montant avancé (débours, sans TVA) + les frais de gestion."""
    what = "Forfait post-stationnement" if fine.kind == "fps" else "Contravention"
    when = f" du {fine.offense_at:%d/%m/%Y}" if fine.offense_at else ""
    lines = [{"label": f"{what} n° {fine.notice_number}{when} — {fine.plate} (débours)", "quantity": 1,
              "unit_price": fine.amount or 0, "vat_rate": 0}]
    fee = float(settings.get(org, "fine_admin_fee") or 0)
    if fee:
        lines.append({"label": "Frais de gestion de contravention", "quantity": 1, "unit_price": fee, "vat_rate": 20})
    return lines


class LocalProvider:
    """Factures PDF générées par l'application, numérotées en continu par année."""

    def create(self, db: Session, org: Organization, customer: Customer, lines: list[dict], label: str) -> dict:
        year = now().year
        prefix = f"{settings.get(org, 'invoice_prefix') or 'F'}-{year}-"
        count = db.query(Invoice).filter(Invoice.org_id == org.id, Invoice.provider == "local",
                                         Invoice.number.like(prefix + "%")).count()
        return {"external_id": "", "number": f"{prefix}{count + 1:04d}", "url": "", "status": "final"}


class PennylaneProvider:
    """API Pennylane v2 (https://app.pennylane.com/api/external/v2), jeton Bearer.

    Corps des requêtes écrits d'après la documentation publique v2 (company_customers, customer_invoices) ;
    à valider avec un jeton réel, Pennylane faisant évoluer son API en 2026.
    """

    BASE = "https://app.pennylane.com/api/external/v2"
    VAT = {20: "FR_200", 10: "FR_100", 5.5: "FR_55", 2.1: "FR_21", 0: "exempt"}

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def _post(self, org: Organization, path: str, body: dict) -> dict:
        try:
            with httpx.Client(transport=self.transport, timeout=30) as http:
                r = http.post(self.BASE + path, json=body, headers={
                    "Authorization": f"Bearer {settings.get(org, 'pennylane_token')}", "Accept": "application/json"})
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as exc:
            raise InvoicingError(f"Pennylane a refusé la requête ({exc.response.status_code}) : {exc.response.text[:300]}") from exc
        except httpx.HTTPError as exc:
            raise InvoicingError(f"Pennylane injoignable : {exc}") from exc

    def _customer_id(self, db: Session, org: Organization, c: Customer) -> int:
        if (c.external_ids or {}).get("pennylane"):
            return c.external_ids["pennylane"]
        address = {"address": c.address, "postal_code": c.postal_code, "city": c.city, "country_alpha2": "FR"}
        if c.company_name:
            body = {"name": c.company_name, "billing_address": address, "emails": [c.email] if c.email else [],
                    "phone": c.phone, "reg_no": c.siret, "vat_number": c.vat_number}
            created = self._post(org, "/company_customers", {k: v for k, v in body.items() if v})
        else:
            body = {"first_name": c.first_name, "last_name": c.last_name, "billing_address": address,
                    "emails": [c.email] if c.email else [], "phone": c.phone}
            created = self._post(org, "/individual_customers", {k: v for k, v in body.items() if v})
        c.external_ids = {**(c.external_ids or {}), "pennylane": created["id"]}
        flag_modified(c, "external_ids")
        db.commit()
        return created["id"]

    def create(self, db: Session, org: Organization, customer: Customer, lines: list[dict], label: str) -> dict:
        if not settings.has(org, "pennylane_token"):
            raise InvoicingError("Jeton Pennylane non configuré (Réglages → Facturation).")
        today = now().date()
        draft = bool(settings.get(org, "invoice_draft"))
        body = {
            "customer_id": self._customer_id(db, org, customer),
            "date": today.isoformat(), "deadline": (today + timedelta(days=30)).isoformat(),
            "draft": draft, "pdf_invoice_subject": label,
            "invoice_lines": [{"label": l["label"], "quantity": l["quantity"], "unit": "piece",
                               "raw_currency_unit_price": f"{l['unit_price']:.2f}",
                               "vat_rate": self.VAT.get(l["vat_rate"], "FR_200")} for l in lines],
        }
        inv = self._post(org, "/customer_invoices", body)
        return {"external_id": str(inv.get("id", "")), "number": inv.get("invoice_number") or "",
                "url": inv.get("public_file_url") or "", "status": "draft" if draft else "final"}


PROVIDERS = {"local": LocalProvider(), "pennylane": PennylaneProvider()}
PROVIDER_LABELS = {"local": "PDF généré par l'application", "pennylane": "Pennylane"}


def create_invoice(db: Session, org: Organization, customer: Customer, lines: list[dict], label: str,
                   booking: Booking | None = None) -> Invoice:
    if not customer:
        raise InvoicingError("Aucun client à facturer.")
    name = settings.get(org, "invoice_provider")
    result = PROVIDERS[name].create(db, org, customer, lines, label)
    ht, ttc = totals(lines)
    invoice = Invoice(org_id=org.id, customer_id=customer.id, booking_id=booking.id if booking else None,
                      provider=name, label=label, lines=lines, amount_ht=ht, amount_ttc=ttc, **result)
    db.add(invoice)
    db.commit()
    return invoice

"""Modèle de données. Chaque table métier porte un org_id : l'application est multi-société (prête pour le SaaS)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, cast, event, func, select
from sqlalchemy.orm import Mapped, mapped_column, object_session, relationship

from .db import Base
from .timeutil import now

ROLES = {"admin": "Administrateur", "manager": "Gestionnaire", "viewer": "Lecture seule"}


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Réglages : identité société, intégrations (jetons chiffrés), assistant… voir services/settings.py
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    webhook_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    active: Mapped[bool] = mapped_column(default=True)
    api_token_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)  # extension Chrome
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    org: Mapped[Organization] = relationship()


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    plate: Mapped[str] = mapped_column(String(20), index=True)
    brand: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(60), default="")
    address: Mapped[str] = mapped_column(String(300), default="")  # lieu de prise en charge
    lock_code: Mapped[str] = mapped_column(String(20), default="")  # code du Master Lock (boîte à clés)
    daily_rate: Mapped[float] = mapped_column(Float, default=0)  # tarif HT / jour (location directe)
    km_included: Mapped[int] = mapped_column(Integer, default=200)  # km inclus / jour
    extra_km_price: Mapped[float] = mapped_column(Float, default=0.30)
    deposit: Mapped[float] = mapped_column(Float, default=1000)
    monthly_cost: Mapped[float] = mapped_column(Float, default=0)  # coûts fixes mensuels (leasing, assurance…)
    # Décompte du carburant à la restitution : 0 = non renseigné (seul l'écart de niveau est indiqué au client)
    fuel_tank_liters: Mapped[float] = mapped_column(Float, default=0)  # capacité du réservoir, en litres
    fuel_price: Mapped[float] = mapped_column(Float, default=0)  # prix du litre refacturé / remboursé, TTC
    getaround_car_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    @property
    def label(self) -> str:
        return f"{self.name} · {self.plate}"


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    company_name: Mapped[str] = mapped_column(String(200), default="")
    siret: Mapped[str] = mapped_column(String(20), default="")
    vat_number: Mapped[str] = mapped_column(String(20), default="")
    first_name: Mapped[str] = mapped_column(String(100), default="")
    last_name: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    postal_code: Mapped[str] = mapped_column(String(10), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    country: Mapped[str] = mapped_column(String(60), default="France")
    birth_date: Mapped[Optional[date]] = mapped_column(Date)
    license_number: Mapped[str] = mapped_column(String(40), default="")
    license_date: Mapped[Optional[date]] = mapped_column(Date)
    license_country: Mapped[str] = mapped_column(String(60), default="France")
    getaround_user_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    external_ids: Mapped[dict] = mapped_column(JSON, default=dict)  # ex. {"pennylane": 123}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    @property
    def person(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def label(self) -> str:
        return self.company_name or self.person or f"Client #{self.id}"


class Booking(Base):
    """Une location, directe (hors plateforme) ou synchronisée depuis Getaround."""

    __tablename__ = "bookings"
    __table_args__ = (UniqueConstraint("org_id", "source", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"))
    source: Mapped[str] = mapped_column(String(20), default="direct")  # direct | getaround
    external_id: Mapped[Optional[str]] = mapped_column(String(40))  # id de location Getaround
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")  # confirmed|in_progress|completed|cancelled
    price_ht: Mapped[float] = mapped_column(Float, default=0)  # direct : prix HT ; Getaround : revenu propriétaire
    vat_rate: Mapped[float] = mapped_column(Float, default=20)
    km_included: Mapped[int] = mapped_column(Integer, default=0)
    deposit: Mapped[float] = mapped_column(Float, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    access_code: Mapped[Optional[str]] = mapped_column(String(12), index=True)  # code client envoyé par SMS
    code_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # acceptation du contrat par le client
    accepted_by: Mapped[str] = mapped_column(String(200), default="")
    accepted_ip: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # Reprise d'historique : location saisie après coup (contrat signé hors application). Jamais de code d'accès,
    # de SMS / e-mail, de blocage du calendrier Getaround ni de demande d'état des lieux.
    historical: Mapped[bool] = mapped_column(default=False)
    contract_ref: Mapped[str] = mapped_column(String(60), default="")  # n° du contrat d'origine (ex. SAH-2026-001)
    # Getaround : retenues déduites par Getaround (carburant, frais…). Revenu brut = price_ht (net) + deductions.
    deductions: Mapped[Optional[float]] = mapped_column(Float)

    vehicle: Mapped[Vehicle] = relationship()
    customer: Mapped[Optional[Customer]] = relationship()
    inspections: Mapped[list["Inspection"]] = relationship(back_populates="booking", cascade="all, delete-orphan")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="booking")

    def inspection(self, kind: str) -> Optional["Inspection"]:
        return next((i for i in self.inspections if i.kind == kind), None)

    @property
    def contract_stage(self) -> str:
        """Étape du contrat, pour nommer le fichier : un PDF différent à chaque étape (le téléphone ne ressert pas l'ancien)."""
        return "final" if self.inspection("checkout") else "depart" if self.inspection("checkin") else "initial"

    @property
    def contract_version(self) -> str:
        stamps = [f"{i.kind}{i.created_at:%Y%m%d%H%M%S}{len(i.photos)}" for i in sorted(self.inspections, key=lambda i: i.kind)]
        return "-".join(stamps) or "0"

    @property
    def contract_filename(self) -> str:
        return f"contrat-{self.reference}-{self.contract_stage}.pdf"

    @property
    def effective_status(self) -> str:
        """Statut affiché : annulée si annulée, sinon déduit des dates.

        Un état des lieux peut faire avancer le statut plus tôt (retour anticipé = terminée), jamais le faire reculer.
        Le statut stocké n'est pas réécrit : le parcours client s'en sert (retour en retard après la fin prévue).
        """
        if self.status == "cancelled":
            return "cancelled"
        t = now()
        by_dates = "completed" if self.ends_at < t else "in_progress" if self.starts_at <= t else "confirmed"
        order = ("confirmed", "in_progress", "completed")
        stored = self.status if self.status in order else "confirmed"
        return max(by_dates, stored, key=order.index)

    @property
    def gross_amount(self) -> float | None:
        """Getaround : revenu avant retenues (si les retenues sont connues)."""
        return round(self.price_ht + self.deductions, 2) if self.deductions else None

    @property
    def imported_renter(self) -> str:
        """Nom du locataire saisi lors d'un import manuel Getaround (rangé dans les notes)."""
        return next((line[12:] for line in (self.notes or "").splitlines() if line.startswith("Locataire : ")), "")

    @property
    def imported_km(self) -> int | None:
        value = next((line[15:] for line in (self.notes or "").splitlines() if line.startswith("Km parcourus : ")), "")
        return int(value) if value.isdigit() else None

    @property
    def renter_label(self) -> str:
        return self.customer.label if self.customer else self.imported_renter

    @property
    def reference(self) -> str:
        return f"GA-{self.external_id}" if self.source == "getaround" else f"LOC-{self.id:05d}"

    @property
    def days(self) -> int:
        """Nombre de jours facturés (toute tranche de 24 h entamée est due)."""
        seconds = (self.ends_at - self.starts_at).total_seconds()
        return max(1, int(-(-seconds // 86400)))

    @property
    def price_ttc(self) -> float:
        return round(self.price_ht * (1 + self.vat_rate / 100), 2)


class Inspection(Base):
    """État des lieux de départ (checkin) ou de retour (checkout)."""

    __tablename__ = "inspections"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # checkin | checkout
    mileage: Mapped[Optional[int]] = mapped_column(Integer)
    fuel_level: Mapped[Optional[int]] = mapped_column(Integer)  # en %
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    booking: Mapped[Booking] = relationship(back_populates="inspections")
    photos: Mapped[list["Photo"]] = relationship(cascade="all, delete-orphan")


class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspections.id"), index=True)
    category: Mapped[str] = mapped_column(String(10))  # exterior | interior
    path: Mapped[str] = mapped_column(String(300))  # relatif à DATA_DIR


class Message(Base):
    """Message de la messagerie Getaround (entrant ou sortant) et décision de l'assistant."""

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("org_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))  # in | out
    content: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # in : new → auto_replied | draft | escalated | handled ; out : sent
    status: Mapped[str] = mapped_column(String(20), default="new")
    ai_draft: Mapped[str] = mapped_column(Text, default="")
    ai_reason: Mapped[str] = mapped_column(Text, default="")
    by_ai: Mapped[bool] = mapped_column(default=False)
    booking: Mapped[Booking] = relationship()


class Fine(Base):
    """Avis de contravention ou forfait post-stationnement (FPS) déposé dans le dossier."""

    __tablename__ = "fines"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(300))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="amende")  # amende | fps
    notice_number: Mapped[str] = mapped_column(String(60), default="")
    plate: Mapped[str] = mapped_column(String(20), default="")
    offense_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    offense_type: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    amount: Mapped[Optional[float]] = mapped_column(Float)
    vehicle_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vehicles.id"))
    booking_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bookings.id"))
    # to_review (lecture incomplète) | unmatched (aucune location) | matched | done (désigné / payé / refacturé)
    status: Mapped[str] = mapped_column(String(20), default="to_review")
    extraction: Mapped[str] = mapped_column(String(20), default="")  # claude | regex
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    vehicle: Mapped[Optional[Vehicle]] = relationship()
    booking: Mapped[Optional[Booking]] = relationship()


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    booking_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bookings.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    provider: Mapped[str] = mapped_column(String(20))  # pennylane | local
    external_id: Mapped[str] = mapped_column(String(60), default="")
    number: Mapped[str] = mapped_column(String(60), default="")
    label: Mapped[str] = mapped_column(String(300), default="")
    amount_ht: Mapped[float] = mapped_column(Float, default=0)
    amount_ttc: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | final
    url: Mapped[str] = mapped_column(String(500), default="")
    lines: Mapped[list] = mapped_column(JSON, default=list)
    issued_on: Mapped[date] = mapped_column(Date, default=lambda: now().date())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    booking: Mapped[Optional[Booking]] = relationship(back_populates="invoices")
    customer: Mapped[Customer] = relationship()


class Expense(Base):
    """Coût variable (carburant, entretien, réparation…). Les coûts fixes sont sur le véhicule."""

    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    vehicle_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vehicles.id"))
    spent_on: Mapped[date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(40), default="Autre")
    label: Mapped[str] = mapped_column(String(200), default="")
    amount: Mapped[float] = mapped_column(Float)  # HT
    vehicle: Mapped[Optional[Vehicle]] = relationship()


class EventLog(Base):
    """Journal des webhooks déjà traités (idempotence) et des envois de SMS."""

    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    key: Mapped[str] = mapped_column(String(200), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


@event.listens_for(Booking, "before_insert")
def _never_reuse_booking_ids(mapper, connection, target):
    """SQLite réattribue le plus grand identifiant s'il a été supprimé : la location suivante reprendrait alors la
    référence LOC-xxxxx d'un contrat déjà signé. On part donc après le plus grand identifiant jamais attribué."""
    if target.id is not None:
        return
    highest = connection.execute(select(func.max(Booking.id))).scalar() or 0
    deleted = connection.execute(select(func.max(cast(EventLog.key, Integer)))
                                 .where(EventLog.kind == "booking_deleted")).scalar() or 0
    # Plusieurs locations dans le même enregistrement (import) : la base ne les voit pas encore, on retient le dernier
    # numéro attribué. Au pire (enregistrement annulé), un numéro est sauté, jamais réutilisé.
    memo = object_session(target).info
    target.id = max(highest, deleted, memo.get("last_booking_id", 0)) + 1
    memo["last_booking_id"] = target.id

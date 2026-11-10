"""Modèle de données. Chaque table métier porte un org_id : l'application est multi-société (prête pour le SaaS)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    vehicle: Mapped[Vehicle] = relationship()
    customer: Mapped[Optional[Customer]] = relationship()
    inspections: Mapped[list["Inspection"]] = relationship(back_populates="booking", cascade="all, delete-orphan")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="booking")

    def inspection(self, kind: str) -> Optional["Inspection"]:
        return next((i for i in self.inspections if i.kind == kind), None)

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

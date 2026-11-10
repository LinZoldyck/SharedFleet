"""Indicateurs de pilotage : chiffre d'affaires, coûts, marge, taux d'occupation."""
import math
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Booking, Expense, Fine, Message, Vehicle
from ..timeutil import now

MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
PERIODS = {"mois": "Ce mois-ci", "precedent": "Mois dernier", "annee": "12 derniers mois"}


def _month_start(d: date, offset: int = 0) -> datetime:
    index = d.year * 12 + d.month - 1 + offset
    return datetime(index // 12, index % 12 + 1, 1)


def period_bounds(period: str) -> tuple[datetime, datetime]:
    today = now().date()
    if period == "precedent":
        return _month_start(today, -1), _month_start(today)
    if period == "annee":
        return _month_start(today, -11), _month_start(today, 1)
    return _month_start(today), _month_start(today, 1)


def _overlap_hours(b: Booking, start: datetime, end: datetime) -> float:
    return max(0.0, (min(b.ends_at, end) - max(b.starts_at, start)).total_seconds() / 3600)


def _fixed_costs(vehicles: list[Vehicle], start: datetime, end: datetime) -> float:
    """Coûts fixes mensuels au prorata des jours de la période (à partir de l'ajout du véhicule)."""
    total = 0.0
    for v in vehicles:
        begin = max(start, datetime.combine(v.created_at.date(), datetime.min.time()))
        days = max(0, (end - begin).days)
        total += v.monthly_cost * days / 30.4375
    return total


def compute(db: Session, org_id: int, period: str = "mois") -> dict:
    start, end = period_bounds(period)
    t = now()
    elapsed_end = min(end, t)  # on ne mesure l'occupation et les coûts fixes que sur le temps écoulé
    vehicles = db.query(Vehicle).filter_by(org_id=org_id, active=True).all()
    bookings = db.query(Booking).filter(Booking.org_id == org_id, Booking.status != "cancelled").all()
    in_period = [b for b in bookings if start <= b.starts_at < end]

    revenue = {"getaround": 0.0, "direct": 0.0}
    for b in in_period:
        revenue[b.source] += b.price_ht
    total_revenue = sum(revenue.values())
    expenses = db.query(Expense).filter(Expense.org_id == org_id, Expense.spent_on >= start.date(),
                                        Expense.spent_on < end.date()).all()
    variable = sum(e.amount for e in expenses)
    fixed = _fixed_costs(vehicles, start, elapsed_end)
    costs = variable + fixed
    by_category: dict[str, float] = {}
    for e in expenses:
        by_category[e.category] = by_category.get(e.category, 0) + e.amount
    if fixed:
        by_category["Coûts fixes"] = fixed

    span_hours = max(1.0, (elapsed_end - start).total_seconds() / 3600)
    per_vehicle = []
    for v in vehicles:
        mine = [b for b in bookings if b.vehicle_id == v.id]
        hours = sum(_overlap_hours(b, start, elapsed_end) for b in mine)
        rev = sum(b.price_ht for b in mine if start <= b.starts_at < end)
        cost = _fixed_costs([v], start, elapsed_end) + sum(e.amount for e in expenses if e.vehicle_id == v.id)
        per_vehicle.append({"vehicle": v, "occupancy": min(100.0, 100 * hours / span_hours), "revenue": rev,
                            "costs": cost, "margin": rev - cost,
                            "count": sum(1 for b in mine if start <= b.starts_at < end)})
    occupancy = sum(x["occupancy"] for x in per_vehicle) / len(per_vehicle) if per_vehicle else 0.0
    rented_days = sum(_overlap_hours(b, start, elapsed_end) for b in bookings) / 24

    # Série mensuelle sur 12 mois (pour les graphiques)
    months = []
    for i in range(-11, 1):
        m_start, m_end = _month_start(t.date(), i), _month_start(t.date(), i + 1)
        m_book = [b for b in bookings if m_start <= b.starts_at < m_end]
        m_exp = sum(e.amount for e in db.query(Expense).filter(
            Expense.org_id == org_id, Expense.spent_on >= m_start.date(), Expense.spent_on < m_end.date()))
        g = sum(b.price_ht for b in m_book if b.source == "getaround")
        d = sum(b.price_ht for b in m_book if b.source == "direct")
        c = m_exp + _fixed_costs(vehicles, m_start, min(m_end, t))
        months.append({"label": f"{MONTHS[m_start.month - 1]} {m_start.year % 100:02d}", "short": MONTHS[m_start.month - 1],
                       "getaround": g, "direct": d, "revenue": g + d, "costs": c, "margin": g + d - c})

    upcoming = sorted((b for b in bookings if b.ends_at >= t and b.status in ("confirmed", "in_progress")),
                      key=lambda b: b.starts_at)[:6]
    return {
        "period": period, "start": start, "end": end - timedelta(days=1),
        "revenue": total_revenue, "revenue_by_source": revenue,
        "direct_share": 100 * revenue["direct"] / total_revenue if total_revenue else 0,
        "costs": costs, "fixed_costs": fixed, "variable_costs": variable, "costs_by_category": by_category,
        "margin": total_revenue - costs,
        "margin_rate": 100 * (total_revenue - costs) / total_revenue if total_revenue else 0,
        "occupancy": occupancy, "bookings_count": len(in_period),
        "revenue_per_rented_day": total_revenue / rented_days if rented_days >= 0.5 else 0,
        "per_vehicle": per_vehicle, "months": months, "upcoming": upcoming,
        "todo": {
            "fines": db.query(Fine).filter(Fine.org_id == org_id, Fine.status.in_(("to_review", "matched", "unmatched"))).count(),
            "messages": db.query(Message).filter(Message.org_id == org_id, Message.direction == "in",
                                                 Message.status.in_(("new", "draft", "escalated"))).count(),
        },
    }


def nice_scale(maximum: float, steps: int = 4) -> list[float]:
    """Graduations « rondes » d'un axe allant de 0 à au moins `maximum`."""
    if maximum <= 0:
        return [0, 1]
    raw = maximum / steps
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    return [step * i for i in range(steps + 1)]

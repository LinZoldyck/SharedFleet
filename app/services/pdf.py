"""Génération des PDF : contrat de location (logo en en-tête, photos avant / après) et facture locale."""
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..models import Booking, Inspection, Invoice, Organization
from . import media, settings

INK = colors.HexColor("#0f172a")
MUTED = colors.HexColor("#64748b")
ACCENT = colors.HexColor("#4f46e5")
LINE = colors.HexColor("#e2e8f0")
SOFT = colors.HexColor("#f8fafc")

_base = getSampleStyleSheet()
S = {
    "body": ParagraphStyle("body", parent=_base["Normal"], fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK),
    "small": ParagraphStyle("small", parent=_base["Normal"], fontName="Helvetica", fontSize=7.5, leading=10, textColor=MUTED),
    "terms": ParagraphStyle("terms", parent=_base["Normal"], fontName="Helvetica", fontSize=8, leading=11, textColor=INK, spaceAfter=3),
    "h1": ParagraphStyle("h1", parent=_base["Normal"], fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=INK),
    "h2": ParagraphStyle("h2", parent=_base["Normal"], fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=ACCENT, spaceBefore=10, spaceAfter=4),
    "label": ParagraphStyle("label", parent=_base["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=10, textColor=MUTED),
    "right": ParagraphStyle("right", parent=_base["Normal"], fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=INK, alignment=2),
}


def eur(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} €".replace(",", " ").replace(".", ",")


def fdt(dt) -> str:
    return dt.strftime("%d/%m/%Y à %Hh%M") if dt else "—"


def p(text, style="body") -> Paragraph:
    return Paragraph(escape(str(text or "—")).replace("\n", "<br/>"), S[style])


def _fit_image(path, max_w, max_h) -> Image:
    w, h = ImageReader(str(path)).getSize()
    ratio = min(max_w / w, max_h / h)
    return Image(str(path), width=w * ratio, height=h * ratio)


def _header(org: Organization, title: str, subtitle: str):
    g = lambda k: settings.get(org, k)  # noqa: E731
    logo = media.absolute(g("logo_path"))
    left = _fit_image(logo, 48 * mm, 22 * mm) if logo else p(g("company_name") or org.name, "h1")
    identity = [(g("company_name") or org.name) if logo else "", g("company_address"),
                f"{g('company_postal_code')} {g('company_city')}".strip(),
                f"SIRET {g('company_siret')}" if g("company_siret") else "",
                f"TVA {g('company_vat')}" if g("company_vat") else "",
                " · ".join(x for x in (g("company_phone"), g("company_email")) if x)]
    right = Paragraph("<br/>".join(escape(x) for x in identity if x), S["right"])
    head = Table([[left, right]], colWidths=[80 * mm, 94 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT),
                              ("BOTTOMPADDING", (0, 0), (-1, 0), 8)]))
    return [head, Spacer(1, 8), p(title, "h1"), p(subtitle, "small"), Spacer(1, 4)]


def _grid(rows, widths, header=False):
    t = Table(rows, colWidths=widths)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
             ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE), ("TOPPADDING", (0, 0), (-1, -1), 5),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (-1, -1), 6)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), SOFT))
    t.setStyle(TableStyle(style))
    return t


def _field(label, value):
    return [p(label.upper(), "label"), p(value)]


def _photos(inspection: Inspection):
    """Grille de photos (3 par ligne) d'un état des lieux."""
    flow = []
    for category, title in (("exterior", "Extérieur"), ("interior", "Intérieur")):
        paths = [media.absolute(ph.path) for ph in inspection.photos if ph.category == category]
        paths = [x for x in paths if x]
        if not paths:
            continue
        flow.append(p(f"{title} — {len(paths)} photo(s)", "label"))
        flow.append(Spacer(1, 3))
        cells = [_fit_image(x, 55 * mm, 42 * mm) for x in paths]
        rows = [cells[i:i + 3] + [""] * (3 - len(cells[i:i + 3])) for i in range(0, len(cells), 3)]
        t = Table(rows, colWidths=[58 * mm] * 3)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        flow += [t, Spacer(1, 6)]
    return flow


def _inspection_block(title: str, inspection: Inspection | None):
    flow = [p(title, "h2")]
    if not inspection:
        flow.append(p("Non réalisé à la date d'édition de ce document.", "small"))
        return flow
    fuel = f"{inspection.fuel_level} %" if inspection.fuel_level is not None else "—"
    km = f"{inspection.mileage:,} km".replace(",", " ") if inspection.mileage is not None else "—"
    flow.append(_grid([[_field("Date", fdt(inspection.created_at)), _field("Kilométrage", km),
                        _field("Carburant", fuel)],
                       [_field("Observations", inspection.notes or "Aucune réserve"), "", ""]],
                      [58 * mm] * 3))
    flow[-1].setStyle(TableStyle([("SPAN", (0, 1), (2, 1))]))
    flow.append(Spacer(1, 6))
    flow += _photos(inspection)
    return flow


def _footer(org):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, settings.get(org, "company_name") or org.name)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()
    return draw


def _build(org, story, title) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm,
                            bottomMargin=18 * mm, title=title, author=settings.get(org, "company_name") or org.name)
    doc.build(story, onFirstPage=_footer(org), onLaterPages=_footer(org))
    return buf.getvalue()


def contract_pdf(org: Organization, booking: Booking) -> bytes:
    v, c = booking.vehicle, booking.customer
    g = lambda k: settings.get(org, k)  # noqa: E731
    story = _header(org, f"Contrat de location n° {booking.reference}", f"Édité le {fdt(booking.created_at)}")

    lessor = "\n".join(x for x in (g("company_name") or org.name, g("company_address"),
                                   f"{g('company_postal_code')} {g('company_city')}".strip(),
                                   f"SIRET {g('company_siret')}" if g("company_siret") else "") if x)
    if c:
        lessee = "\n".join(x for x in (
            c.company_name, f"Représenté par {c.person}" if c.company_name and c.person else c.person,
            c.address, f"{c.postal_code} {c.city}".strip(), f"SIRET {c.siret}" if c.siret else "",
            f"Tél. {c.phone}" if c.phone else "", c.email,
            f"Permis n° {c.license_number}" + (f" délivré le {c.license_date:%d/%m/%Y}" if c.license_date else "")
            if c.license_number else "") if x)
    else:
        lessee = "—"
    story.append(_grid([[p("LE LOUEUR", "label"), p("LE LOCATAIRE", "label")], [p(lessor), p(lessee)]],
                       [87 * mm, 87 * mm], header=True))

    story.append(p("Véhicule", "h2"))
    story.append(_grid([[_field("Désignation", f"{v.brand} {v.model}".strip() or v.name),
                         _field("Immatriculation", v.plate), _field("Lieu de prise en charge", v.address)]],
                       [58 * mm] * 3))

    story.append(p("Location", "h2"))
    km = f"{booking.km_included:,} km".replace(",", " ") if booking.km_included else "Illimité"
    story.append(_grid([
        [_field("Départ", fdt(booking.starts_at)), _field("Retour", fdt(booking.ends_at)),
         _field("Durée", f"{booking.days} jour(s)")],
        [_field("Kilométrage inclus", km), _field("Km supplémentaire", f"{eur(v.extra_km_price)} HT"),
         _field("Dépôt de garantie", eur(booking.deposit))],
        [_field("Prix HT", eur(booking.price_ht)), _field(f"TVA {booking.vat_rate:g} %", eur(booking.price_ttc - booking.price_ht)),
         _field("Prix TTC", eur(booking.price_ttc))]], [58 * mm] * 3))
    if booking.notes:
        story += [Spacer(1, 4), p(f"Conditions particulières : {booking.notes}")]

    story.append(p("Conditions générales", "h2"))
    for line in (g("contract_terms") or "").splitlines():
        if line.strip():
            story.append(p(line.strip(), "terms"))

    if booking.accepted_at:
        accept = (f"Contrat accepté électroniquement par {booking.accepted_by} le {fdt(booking.accepted_at)}"
                  f" (adresse IP {booking.accepted_ip}).")
    else:
        accept = "En attente d'acceptation par le locataire (réalisée dans l'application lors de l'état des lieux de départ)."
    story.append(KeepTogether([p("Acceptation", "h2"), _grid([[_field("Le loueur", g("company_name") or org.name), _field("Le locataire", accept)]],
                                     [60 * mm, 114 * mm])]))

    story.append(PageBreak())
    story += _inspection_block("État des lieux de départ — photos avant location", booking.inspection("checkin"))
    story += _inspection_block("État des lieux de retour — photos après location", booking.inspection("checkout"))
    cin, cout = booking.inspection("checkin"), booking.inspection("checkout")
    if cin and cout and cin.mileage is not None and cout.mileage is not None:
        driven = cout.mileage - cin.mileage
        extra = max(0, driven - booking.km_included) if booking.km_included else 0
        story.append(p(f"Distance parcourue : {driven} km" + (
            f" — dont {extra} km supplémentaires, soit {eur(extra * v.extra_km_price)} HT." if extra else ".")))
    return _build(org, story, f"Contrat {booking.reference}")


def invoice_pdf(org: Organization, invoice: Invoice) -> bytes:
    c = invoice.customer
    story = _header(org, f"Facture n° {invoice.number}", f"Date d'émission : {invoice.issued_on:%d/%m/%Y}")
    client = "\n".join(x for x in (c.label, c.address, f"{c.postal_code} {c.city}".strip(),
                                   f"SIRET {c.siret}" if c.siret else "", f"TVA {c.vat_number}" if c.vat_number else "") if x)
    story.append(_grid([[p("FACTURÉ À", "label")], [p(client)]], [174 * mm], header=True))
    story.append(Spacer(1, 10))
    rows = [[p("DÉSIGNATION", "label"), p("QTÉ", "label"), p("PU HT", "label"), p("TVA", "label"), p("TOTAL HT", "label")]]
    for line in invoice.lines:
        rows.append([p(line["label"]), p(f"{line['quantity']:g}"), p(eur(line["unit_price"])),
                     p(f"{line['vat_rate']:g} %"), p(eur(line["quantity"] * line["unit_price"]))])
    story.append(_grid(rows, [84 * mm, 16 * mm, 26 * mm, 18 * mm, 30 * mm], header=True))
    story.append(Spacer(1, 8))
    totals = Table([[p("Total HT", "label"), p(eur(invoice.amount_ht))],
                    [p("TVA", "label"), p(eur(invoice.amount_ttc - invoice.amount_ht))],
                    [p("TOTAL TTC", "label"), Paragraph(f"<b>{eur(invoice.amount_ttc)}</b>", S["body"])]],
                   colWidths=[30 * mm, 30 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([("LINEABOVE", (0, 2), (-1, 2), 0.8, INK)]))
    story += [totals, Spacer(1, 14),
              p("Paiement à réception. En cas de retard, pénalités au taux de 3 fois le taux d'intérêt légal et "
                "indemnité forfaitaire pour frais de recouvrement de 40 €.", "small")]
    return _build(org, story, f"Facture {invoice.number}")

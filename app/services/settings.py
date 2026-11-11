"""Réglages par société, stockés dans Organization.settings. Les secrets sont chiffrés au repos."""
from sqlalchemy.orm.attributes import flag_modified

from ..models import Organization
from ..security import decrypt, encrypt

SECRET_KEYS = {"getaround_token", "getaround_webhook_secret", "anthropic_api_key", "twilio_token", "pennylane_token",
               "sms_http_token", "smtp_password"}

DEFAULT_TERMS = """1. Objet — Le loueur met à disposition du locataire le véhicule désigné, pour la durée et au prix indiqués au présent contrat.
2. Conducteur — Le véhicule ne peut être conduit que par le locataire ou les conducteurs qu'il désigne, titulaires d'un permis de conduire valide correspondant à la catégorie du véhicule.
3. État du véhicule — Un état des lieux contradictoire avec photographies est réalisé au départ et au retour. À défaut de réserve au départ, le véhicule est réputé pris en bon état.
4. Utilisation — Le locataire s'engage à utiliser le véhicule en bon père de famille, à respecter le Code de la route, la charge utile, et à ne pas sous-louer le véhicule ni le sortir du territoire sans accord écrit.
5. Kilométrage et carburant — Le kilométrage inclus figure au contrat ; tout kilomètre supplémentaire est facturé au tarif indiqué. Le véhicule est restitué avec le même niveau de carburant qu'au départ ; à défaut, le complément est facturé.
6. Restitution — Le véhicule est restitué à la date, à l'heure et au lieu convenus. Tout retard entraîne la facturation d'une journée supplémentaire par tranche de 24 heures entamée.
7. Dépôt de garantie et dommages — Le locataire est responsable des dommages, vols et pertes survenus pendant la location, dans la limite de la franchise ou du dépôt de garantie indiqué, sauf faute lourde ou usage non conforme.
8. Infractions — Le locataire est responsable des infractions commises pendant la location. Le loueur communiquera son identité aux autorités (désignation du conducteur). Les forfaits post-stationnement et frais de gestion associés sont refacturés au locataire.
9. Assurance — Le véhicule est assuré en responsabilité civile. Le locataire déclare tout sinistre au loueur sous 24 heures.
10. Acceptation — L'acceptation électronique du contrat et la réalisation de l'état des lieux dans l'application valent signature."""

DEFAULTS = {
    "company_name": "", "company_address": "", "company_postal_code": "", "company_city": "",
    "company_siret": "", "company_vat": "", "company_phone": "", "company_email": "",
    "logo_path": "", "contract_terms": DEFAULT_TERMS,
    "getaround_block_calendar": True, "getaround_include_insurance": False,
    "assistant_mode": "draft",  # off | draft (brouillon à valider) | auto (envoi automatique)
    "assistant_instructions": "",
    "code_by_sms": True, "code_by_email": True,  # canaux d'envoi du code d'accès
    "email_provider": "off",  # off | console | smtp
    "smtp_host": "", "smtp_port": "", "smtp_security": "starttls", "smtp_user": "", "email_from": "", "email_from_name": "",
    "sms_provider": "console",  # console | twilio | http
    "twilio_sid": "", "twilio_from": "", "sms_http_url": "",
    "invoice_provider": "local",  # local | pennylane
    "invoice_prefix": "F", "invoice_draft": True,
    "fine_admin_fee": 25.0,
}


def get(org: Organization, key: str):
    value = (org.settings or {}).get(key, DEFAULTS.get(key, ""))
    if key in SECRET_KEYS and value:
        return decrypt(value)
    return value


def has(org: Organization, key: str) -> bool:
    return bool(get(org, key))


def update(org: Organization, values: dict) -> None:
    settings = dict(org.settings or {})
    for key, value in values.items():
        if key in SECRET_KEYS:
            if value == "":  # champ laissé vide = on garde le secret existant
                continue
            value = "" if value == "-" else encrypt(value)  # « - » efface le secret
        settings[key] = value
    org.settings = settings
    flag_modified(org, "settings")

"""Prise de vue guidée de l'état des lieux : une case par photo, avec une illustration.

Les vues extérieures montrent le véhicule vu de dessus (avant en haut), la position où se placer (pastille appareil
photo et ligne de visée) et, en couleur, les faces qui doivent apparaître sur la photo. Les dessins sont générés ici en
SVG : pas de fichier image à maintenir, rendu net sur tous les écrans, couleurs reprises du thème.
"""
from dataclasses import dataclass, field

from markupsafe import Markup

ACCENT = "#4f46e5"


@dataclass(frozen=True)
class Slot:
    key: str            # nom du champ du formulaire
    category: str       # exterior | interior
    label: str
    hint: str
    required: bool
    camera: tuple = ()  # vues extérieures : position de l'appareil (x, y) dans le plan
    target: tuple = ()  # point visé
    faces: tuple = ()   # faces mises en évidence : front, rear, left, right, mirror_left, mirror_right
    drawing: str = ""   # vues intérieures : dashboard | seats | cargo


SLOTS = [
    Slot("ext_av_g", "exterior", "3/4 avant gauche", "Placez-vous à l'angle avant gauche : l'avant et tout le côté gauche.",
         True, (16, 9), (43.5, 20.5), ("front", "left")),
    Slot("ext_av_d", "exterior", "3/4 avant droit", "Angle avant droit : l'avant et tout le côté droit.",
         True, (104, 9), (76.5, 20.5), ("front", "right")),
    Slot("ext_ar_g", "exterior", "3/4 arrière gauche", "Angle arrière gauche : l'arrière et le côté gauche.",
         True, (16, 94), (42, 86), ("rear", "left")),
    Slot("ext_ar_d", "exterior", "3/4 arrière droit", "Angle arrière droit : l'arrière et le côté droit.",
         True, (104, 94), (78, 86), ("rear", "right")),
    Slot("int_dash", "interior", "Tableau de bord", "Contact mis : le kilométrage et la jauge de carburant doivent être lisibles.",
         True, drawing="dashboard"),
    Slot("ext_face", "exterior", "Face avant", "Bien en face : pare-chocs, calandre et pare-brise.",
         False, (60, 6), (60, 17), ("front",)),
    Slot("ext_arriere", "exterior", "Arrière", "Bien en face : portes arrière, pare-chocs et plaque.",
         False, (60, 95), (60, 87), ("rear",)),
    Slot("ext_retro_g", "exterior", "Rétroviseur gauche", "De près : la coque et le miroir.",
         False, (16, 44), (39.5, 32), ("mirror_left",)),
    Slot("ext_retro_d", "exterior", "Rétroviseur droit", "De près : la coque et le miroir.",
         False, (104, 44), (80.5, 32), ("mirror_right",)),
    Slot("int_sieges", "interior", "Sièges avant", "Depuis la portière : sièges et tapis.", False, drawing="seats"),
    Slot("int_chargement", "interior", "Zone de chargement", "Portes arrière ouvertes : plancher et parois.",
         False, drawing="cargo"),
]
BY_KEY = {s.key: s for s in SLOTS}
REQUIRED = [s for s in SLOTS if s.required]

_FACES = {
    "front": "M47 18 H73",
    "rear": "M42 86 H78",
    "left": "M44 24 V38 M42 38 V86",
    "right": "M76 24 V38 M78 38 V86",
}


def _van(faces: tuple) -> str:
    hl = "".join(f'<path d="{_FACES[f]}" stroke="{ACCENT}" stroke-width="3.2" stroke-linecap="round" fill="none"/>'
                 for f in faces if f in _FACES)
    mirrors = "".join(
        f'<rect x="{x}" y="30" width="5" height="4" rx="1" fill="{ACCENT if f in faces else "#cbd5e1"}" '
        f'stroke="{ACCENT if f in faces else "#94a3b8"}" stroke-width="1"/>'
        for x, f in ((37, "mirror_left"), (78, "mirror_right")))
    return (
        # roues, carrosserie (cabine + caisse), pare-brise, séparation des portes arrière, repère « AV »
        '<g fill="#475569"><rect x="39.5" y="24" width="3" height="9" rx="1"/><rect x="77.5" y="24" width="3" height="9" rx="1"/>'
        '<rect x="39.5" y="70" width="3" height="10" rx="1"/><rect x="77.5" y="70" width="3" height="10" rx="1"/></g>'
        '<path d="M44 38 V25 Q44 18 51 18 H69 Q76 18 76 25 V38 Z" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.2"/>'
        '<rect x="42" y="36" width="36" height="50" rx="3" fill="#f8fafc" stroke="#94a3b8" stroke-width="1.2"/>'
        '<path d="M47.5 33 Q60 29 72.5 33 L71 36 H49 Z" fill="#dbeafe" stroke="#94a3b8" stroke-width=".8"/>'
        '<path d="M60 78 V86" stroke="#cbd5e1" stroke-width="1"/>'
        '<text x="60" y="27" text-anchor="middle" font-size="6" font-family="system-ui,sans-serif" fill="#94a3b8">AV</text>'
        + mirrors + hl)


def _camera(cam: tuple, target: tuple) -> str:
    (x, y), (tx, ty) = cam, target
    return (f'<path d="M{x} {y} L{tx} {ty}" stroke="{ACCENT}" stroke-width="1.2" stroke-dasharray="2.5 2" opacity=".75"/>'
            f'<circle cx="{x}" cy="{y}" r="6" fill="{ACCENT}"/>'
            f'<rect x="{x - 3.4}" y="{y - 2.2}" width="6.8" height="4.6" rx="1" fill="#fff"/>'
            f'<circle cx="{x}" cy="{y + .1}" r="1.5" fill="{ACCENT}"/>')


_DRAWINGS = {
    # tableau de bord : compteur (aiguille + kilométrage) et jauge de carburant
    "dashboard": (
        '<rect x="10" y="22" width="100" height="56" rx="12" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.2"/>'
        '<circle cx="40" cy="48" r="18" fill="#fff" stroke="#94a3b8" stroke-width="1.2"/>'
        '<path d="M40 48 L52 39" stroke="#475569" stroke-width="2" stroke-linecap="round"/>'
        f'<rect x="27" y="56" width="26" height="8" rx="2" fill="#fff" stroke="{ACCENT}" stroke-width="1.6"/>'
        f'<text x="40" y="62.4" text-anchor="middle" font-size="5.6" font-family="ui-monospace,monospace" fill="{ACCENT}">045210</text>'
        '<circle cx="82" cy="48" r="18" fill="#fff" stroke="#94a3b8" stroke-width="1.2"/>'
        f'<path d="M69 54 A14 14 0 0 1 95 54" stroke="{ACCENT}" stroke-width="2.4" fill="none"/>'
        '<path d="M82 52 L75 41" stroke="#475569" stroke-width="2" stroke-linecap="round"/>'
        '<text x="72" y="59" font-size="6" font-family="system-ui,sans-serif" fill="#64748b">E</text>'
        '<text x="88.5" y="59" font-size="6" font-family="system-ui,sans-serif" fill="#64748b">F</text>'
        '<text x="60" y="90" text-anchor="middle" font-size="6.5" font-family="system-ui,sans-serif" fill="#64748b">km + carburant lisibles</text>'),
    # sièges avant vus de face
    "seats": "".join(
        f'<rect x="{x}" y="20" width="30" height="40" rx="7" fill="#f8fafc" stroke="{ACCENT}" stroke-width="1.6"/>'
        f'<rect x="{x - 3}" y="58" width="36" height="16" rx="5" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.2"/>'
        f'<rect x="{x + 8}" y="10" width="14" height="9" rx="3" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.2"/>'
        for x in (22, 68)) + '<rect x="14" y="80" width="92" height="6" rx="2" fill="#e2e8f0"/>',
    # arrière ouvert : plancher et parois
    "cargo": (
        '<path d="M28 14 H92 V86 H28 Z" fill="#f8fafc" stroke="#94a3b8" stroke-width="1.2"/>'
        f'<path d="M38 24 H82 V80 H38 Z" fill="#fff" stroke="{ACCENT}" stroke-width="1.6"/>'
        f'<path d="M38 80 L28 86 M82 80 L92 86 M38 24 L28 14 M82 24 L92 14" stroke="{ACCENT}" stroke-width="1" opacity=".6"/>'
        '<path d="M28 14 L12 22 V82 L28 86 Z M92 14 L108 22 V82 L92 86 Z" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.2"/>'
        '<rect x="50" y="62" width="20" height="14" rx="1.5" fill="#e2e8f0" stroke="#94a3b8" stroke-width="1"/>'),
}


def illustration(slot: Slot) -> Markup:
    body = _DRAWINGS[slot.drawing] if slot.drawing else _van(slot.faces) + _camera(slot.camera, slot.target)
    return Markup(f'<svg viewBox="0 -2 120 104" role="img" aria-label="{slot.label}" xmlns="http://www.w3.org/2000/svg">'
                  f"{body}</svg>")

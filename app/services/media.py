"""Stockage des fichiers envoyés (photos d'état des lieux, logo)."""
import io
import secrets
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .. import config

MAX_UPLOAD = 15 * 1024 * 1024


class InvalidImage(ValueError):
    pass


def save_image(org_id: int, folder: str, data: bytes, max_side: int = 1600, keep_alpha: bool = False) -> str:
    """Valide, redresse (EXIF), réduit et enregistre l'image. Retourne le chemin relatif à DATA_DIR."""
    if len(data) > MAX_UPLOAD:
        raise InvalidImage("Image trop volumineuse (15 Mo max).")
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_side, max_side))
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidImage("Ce fichier n'est pas une image lisible.") from exc
    target = config.org_dir(org_id, folder)
    if keep_alpha and img.mode in ("RGBA", "LA", "P"):
        path = target / f"{secrets.token_hex(8)}.png"
        img.convert("RGBA").save(path, "PNG")
    else:
        path = target / f"{secrets.token_hex(8)}.jpg"
        img.convert("RGB").save(path, "JPEG", quality=82)
    return str(path.relative_to(config.DATA_DIR))


def absolute(rel_path: str) -> Path | None:
    """Chemin absolu d'un fichier stocké, ou None s'il sort de DATA_DIR ou n'existe pas."""
    if not rel_path:
        return None
    path = (config.DATA_DIR / rel_path).resolve()
    if not path.is_relative_to(config.DATA_DIR) or not path.is_file():
        return None
    return path

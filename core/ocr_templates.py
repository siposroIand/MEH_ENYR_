"""Validated, local fixed-layout OCR templates; coordinates use the displayed page."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4


FIELD_LABELS = {"subject": "Tárgy", "validity": "Hatály / lejárat"}
# PDFium must not be called concurrently by the editor and recognition workers.
PDFIUM_LOCK = RLock()


@dataclass(frozen=True)
class OcrRegion:
    field: str
    page: int
    rect: tuple[float, float, float, float]

    def __post_init__(self):
        if self.field not in FIELD_LABELS:
            raise ValueError("Ismeretlen sablonmező.")
        if type(self.page) is not int or not 1 <= self.page <= 10000:
            raise ValueError("Az oldalszám 1 és 10000 közötti egész szám legyen.")
        if len(self.rect) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
            for v in self.rect
        ):
            raise ValueError("Érvénytelen kijelölési koordináták.")
        x0, y0, x1, y1 = self.rect
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("A kijelölésnek az oldalon belül kell lennie.")
        object.__setattr__(self, "rect", tuple(float(v) for v in self.rect))

    def pixel_box(self, width: int, height: int) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = self.rect
        return (
            max(0, math.floor(x0 * width)), max(0, math.floor(y0 * height)),
            min(width, math.ceil(x1 * width)), min(height, math.ceil(y1 * height)),
        )


@dataclass(frozen=True)
class OcrTemplate:
    id: str
    name: str
    authority: str
    regions: tuple[OcrRegion, ...]

    def __post_init__(self):
        try:
            if str(UUID(self.id)) != self.id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Érvénytelen sablonazonosító.") from exc
        for value in (self.name, self.authority):
            if not isinstance(value, str) or not value.strip() or len(value) > 150:
                raise ValueError("A sablonnév és a hatóság neve kötelező (legfeljebb 150 karakter).")
        if not self.regions or len(self.regions) > 100:
            raise ValueError("Legalább egy, legfeljebb 100 felismerési terület szükséges.")
        keys = set()
        for region in self.regions:
            if not isinstance(region, OcrRegion):
                raise ValueError("Érvénytelen felismerési terület.")
            key = region.field, region.page
            if key in keys:
                raise ValueError("Egy mezőhöz oldalanként egy terület jelölhető ki.")
            keys.add(key)
        object.__setattr__(self, "regions", tuple(self.regions))

    @classmethod
    def create(cls, name: str, authority: str, regions) -> OcrTemplate:
        return cls(str(uuid4()), name.strip(), authority.strip(), tuple(regions))


class TemplateStore:
    """One atomic JSON file per profile; never store source documents or OCR text."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.warnings: list[str] = []

    def load(self) -> list[OcrTemplate]:
        self.warnings = []
        templates = []
        if not self.directory.exists():
            return templates
        for path in sorted(self.directory.glob("*.json")):
            try:
                if path.stat().st_size > 200_000:
                    raise ValueError("Túl nagy sablonfájl.")
                data = json.loads(path.read_text(encoding="utf-8"))
                if type(data["version"]) is not int or data["version"] != 1:
                    raise ValueError("Nem támogatott sablonverzió.")
                regions = tuple(OcrRegion(**r) for r in data["regions"])
                template = OcrTemplate(data["id"], data["name"], data["authority"], regions)
                if path.stem != template.id:
                    raise ValueError("A fájlnév és a sablonazonosító eltér.")
                templates.append(template)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.warnings.append(f"{path.name}: {exc}")
        return sorted(templates, key=lambda t: (t.authority.casefold(), t.name.casefold()))

    def save(self, template: OcrTemplate) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if any(
            other.id != template.id
            and (other.authority.casefold(), other.name.casefold())
            == (template.authority.casefold(), template.name.casefold())
            for other in self.load()
        ):
            raise ValueError("Ennél a hatóságnál már van ilyen nevű sablon; válasszon másik nevet.")
        path = self.directory / f"{template.id}.json"
        pending = self.directory / f".{template.id}.{uuid4()}.pending"
        try:
            with pending.open("x", encoding="utf-8") as stream:
                json.dump({"version": 1, **asdict(template)}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)

    def delete(self, template: OcrTemplate) -> None:
        (self.directory / f"{template.id}.json").unlink(missing_ok=True)


def document_page_count(path: Path) -> int:
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        with PDFIUM_LOCK, closing(pdfium.PdfDocument(str(path))) as document:
            return len(document)
    from PIL import Image
    with Image.open(path) as image:
        return getattr(image, "n_frames", 1)


def render_document_page(path: Path, page_number: int, max_dimension: int = 2200):
    """Return an owned RGB Pillow image, oriented exactly as template recognition."""
    if page_number < 1:
        raise ValueError("Érvénytelen oldalszám.")
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        with PDFIUM_LOCK, closing(pdfium.PdfDocument(str(path))) as document:
            if page_number > len(document):
                raise ValueError("Az oldal nem található a dokumentumban.")
            with closing(document[page_number - 1]) as page:
                scale = min(3, max_dimension / max(page.get_size()))
                with closing(page.render(scale=scale)) as bitmap:
                    with bitmap.to_pil() as image:
                        return image.convert("RGB")
    from PIL import Image, ImageOps
    with Image.open(path) as image:
        image.seek(page_number - 1)
        with ImageOps.exif_transpose(image) as oriented:
            result = oriented.convert("RGB")
            result.thumbnail((max_dimension, max_dimension))
            return result

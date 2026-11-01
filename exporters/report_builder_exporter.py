"""Report-builder exports with one shared column, ordering and formatting model."""
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
import json
import math
import re

from core.report_data import parse_date, validity_type


ALL_COLUMNS = [
    ("telephely", "Telephely"),
    ("kategoria", "Engedélytípus"),
    ("engedely_nev", "Tárgy"),
    ("kiado_hatosag", "Kiadó hatóság"),
    ("hatarozatszam", "Határozat száma"),
    ("modosito_hat", "Módosító határozat"),
    ("letrejott", "Létrejött"),
    ("lejarat", "Hatálya"),
    ("felulvizsgalat", "Felülvizsgálat"),
    ("megjegyzes", "Megjegyzés"),
    ("hulladek_nv_elokez", "NV-Előkezelés"),
    ("hulladek_v_elokez", "V-Előkezelés"),
    ("hulladek_nv_gyujt", "NV-Gyűjtés"),
    ("hulladek_v_gyujt", "V-Gyűjtés"),
]
KEY_TO_HEADER = dict(ALL_COLUMNS)
ALIASES = {"felulvizsgalat_date": "felulvizsgalat", "lejarat_display": "lejarat"}


def _render_str(value):
    return "" if value is None else str(value)


def _fmt_date_full(value):
    parsed = parse_date(value)
    return parsed.strftime("%Y.%m.%d") if parsed else _render_str(value)


def _extract_review_entries(item):
    raw = item.get("felulvizsgalatok")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError as exc:
            raise ValueError("Hibás felülvizsgálati adatok: nem érvényes JSON.") from exc
    if raw is not None and not isinstance(raw, list):
        raise ValueError("Hibás felülvizsgálati adatok: lista szükséges.")
    entries = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            raise ValueError("Hibás felülvizsgálati bejegyzés.")
        if entry.get("type") == "before_expiry":
            days = _review_days(entry.get("days"))
            expiry = parse_date(item.get("lejarat")) if validity_type(item) == "fixed" else None
            if expiry:
                note = f"{days} nappal a lejárat előtt (számított)"
                if entry.get("note"):
                    note += " : " + str(entry["note"])
                entries.append((expiry - timedelta(days=days), note))
            continue
        value = entry.get("date")
        parsed = parse_date(value)
        # Non-date review conditions are retained in the notes block.
        if parsed:
            entries.append((parsed, _render_str(entry.get("note"))))
        elif value:
            raise ValueError(f"Érvénytelen felülvizsgálati dátum: {value}")
    if not entries and not raw:
        single = parse_date(item.get("felulvizsgalat_date"))
        if single:
            entries.append((single, ""))
    if not entries and not raw:
        days = item.get("felulvizsgalat_nap")
        expiry = parse_date(item.get("lejarat")) if validity_type(item) == "fixed" else None
        if days not in (None, "", 0, "0") and expiry:
            try:
                days = int(days)
            except (ValueError, TypeError) as exc:
                raise ValueError("Érvénytelen felülvizsgálati napérték.") from exc
            if days > 0:
                entries.append((expiry - timedelta(days=days), f"{days} nappal a lejárat előtt (számított)"))
    return sorted(set(entries), key=lambda entry: (entry[0], entry[1]))


def _review_days(value):
    try:
        days = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Érvénytelen felülvizsgálati napérték.") from exc
    if days <= 0:
        raise ValueError("A felülvizsgálati napértéknek pozitívnak kell lennie.")
    return days


def _review_texts(item):
    entries = _extract_review_entries(item)
    dates = "\n".join(dict.fromkeys(d.strftime("%Y.%m.%d") for d, _ in entries))
    notes = [f"{d:%Y.%m.%d}" + (f" : {note}" if note else "") for d, note in entries]
    raw = item.get("felulvizsgalatok") or []
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else []
    for entry in raw or []:
        if entry.get("type") == "before_expiry":
            expiry = parse_date(item.get("lejarat")) if validity_type(item) == "fixed" else None
            if expiry is None:
                text = f"{_review_days(entry.get('days'))} nappal a lejárat előtt"
                if entry.get("note"):
                    text += " : " + str(entry["note"])
                notes.append(text)
            continue
        if not entry.get("date"):
            text = entry.get("text") or entry.get("note") or entry.get("condition")
            if text:
                notes.append(str(text))
    if not raw and not entries and item.get("felulvizsgalat_nap") not in (None, "", 0, "0"):
        notes.append(f"{_review_days(item['felulvizsgalat_nap'])} nappal a lejárat előtt")
    return dates, "FELÜLVIZSGÁLATOK:\n" + "\n".join(notes) if notes else ""


def _prepare_row(item, tp_map, columns, include_waste=True):
    row = {}
    reviews = _review_texts(item) if any(c in columns for c in ("felulvizsgalat", "megjegyzes")) else ("", "")
    for key in columns:
        value = item.get(key)
        if key == "telephely":
            value = tp_map.get(item.get("telephely_id"), "Ismeretlen")
        elif key == "kategoria":
            value = value or "Ismeretlen"
        elif key == "hatarozatszam":
            value = _render_str(value).replace("\n", " ")
        elif key == "modosito_hat":
            value = re.sub(r";\s*", "\n", _render_str(value))
        elif key == "letrejott":
            value = _fmt_date_full(value)
        elif key == "lejarat":
            kind = validity_type(item)
            value = {"indefinite": "Határozatlan", "until_change": "Változtatásig"}.get(kind)
            if value is None:
                value = _fmt_date_full(item.get("lejarat"))
        elif key == "felulvizsgalat":
            value = reviews[0]
        elif key == "megjegyzes":
            value = _render_str(value)
            if reviews[1]:
                value = (value + "\n\n" if value else "") + reviews[1]
            if item.get("kategoria") == "Hulladékgazdálkodási engedély" and include_waste:
                parts = [f"{label}: {item[k]}" for k, label in ALL_COLUMNS
                         if k.startswith("hulladek_") and item.get(k) not in (None, "")]
                if parts:
                    value = (value + "\n" if value else "") + "[" + ", ".join(parts) + "]"
        row[key] = "" if value is None else value
    return row


@dataclass
class PreparedGroup:
    name: str
    columns: list[str]
    rows: list[list[Any]]

    @property
    def headers(self):
        return [KEY_TO_HEADER[key] for key in self.columns]


def prepare_report_groups(groups_or_items, telephelyek, columns, include_waste=True, *, prune_empty=True):
    """Canonical data consumed by the preview and both exporters.

    Legacy callers retain dynamic column pruning. The builder disables pruning
    so explicitly selected empty columns remain visible in every group.
    """
    groups = groups_or_items if isinstance(groups_or_items, dict) else {"Összes": groups_or_items or []}
    tp_map = {t["id"]: t.get("nev") or "Ismeretlen" for t in telephelyek}
    result = []
    for name, items in groups.items():
        requested = columns.get(name, columns.get("default", list(KEY_TO_HEADER))) if isinstance(columns, dict) else columns
        if not requested:
            if not prune_empty:
                raise ValueError("Válasszon legalább egy oszlopot.")
            requested = list(KEY_TO_HEADER)
        keys = list(dict.fromkeys(ALIASES.get(key, key) for key in requested))
        if any(key not in KEY_TO_HEADER for key in keys):
            raise ValueError("Ismeretlen kimutatásoszlop.")
        ordered = sorted(items, key=lambda item: (item.get("kategoria") or "").casefold())
        rows = [_prepare_row(item, tp_map, keys, include_waste) for item in ordered]
        if prune_empty and rows:
            essential = {"telephely", "kategoria", "engedely_nev"}
            kept = [key for key in keys if key in essential or any(row[key] not in (None, "") for row in rows)]
            # A group containing only empty selected columns must remain exportable.
            keys = kept or keys
        result.append(PreparedGroup(str(name), keys, [[row[key] for key in keys] for row in rows]))
    return result


def _register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/freefont/FreeSans.ttf", "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        (str(Path(__file__).parent / "fonts/DejaVuSans.ttf"), str(Path(__file__).parent / "fonts/DejaVuSans-Bold.ttf")),
    ]
    for regular, bold in candidates:
        if Path(regular).is_file() and Path(bold).is_file():
            pdfmetrics.registerFont(TTFont("App-Regular", regular))
            pdfmetrics.registerFont(TTFont("App-Bold", bold))
            return "App-Regular", "App-Bold"
    raise RuntimeError("A magyar PDF-exporthoz Unicode betűkészlet szükséges (DejaVu Sans, Liberation Sans vagy Arial).")


def _pdf_text(value):
    return escape(_render_str(value)).replace("\n", "<br/>")


def export_report_pdf(path, groups_or_items, telephelyek, columns, title=None,
                      subtitle=None, logo_path=None, include_waste=True, *, prune_empty=True):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, PageBreak, Image, Spacer, Paragraph

    groups = prepare_report_groups(groups_or_items, telephelyek, columns, include_waste, prune_empty=prune_empty)
    if not groups:
        raise ValueError("Nincs exportálható csoport.")
    regular, bold = _register_fonts()
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4),
                            leftMargin=.6*cm, rightMargin=.6*cm, topMargin=.8*cm, bottomMargin=1.6*cm)
    title_style = ParagraphStyle("title", fontName=bold, fontSize=16, leading=19, alignment=1, spaceAfter=6)
    header_style = ParagraphStyle("header", fontName=bold, fontSize=9, leading=11, alignment=1)
    cell_style = ParagraphStyle("cell", fontName=regular, fontSize=9, leading=11)
    elements = []
    if logo_path:
        image = Image(str(logo_path), width=6*cm, height=2*cm, kind="proportional")
        image.hAlign = "CENTER"
        elements.extend([image, Spacer(1, .2*cm)])
    if title:
        elements.extend([Paragraph(_pdf_text(title), title_style), Spacer(1, .2*cm)])
    for index, group in enumerate(groups):
        if index:
            elements.append(PageBreak())
        elements.extend([Paragraph(_pdf_text(group.name), header_style), Spacer(1, .15*cm)])
        data = [[Paragraph(_pdf_text(label), header_style) for label in group.headers]]
        data.extend([[Paragraph(_pdf_text(value), cell_style) for value in row] for row in group.rows])
        weights = []
        for col, key in enumerate(group.columns):
            longest = max([len(group.headers[col])] + [len(_render_str(row[col])) for row in group.rows])
            base = 3 if key in ("engedely_nev", "megjegyzes") else 1
            weights.append(base * (1 + math.log1p(longest) / 3))
        widths = [doc.width * weight / sum(weights) for weight in weights]
        table = Table(data, colWidths=widths, repeatRows=1, splitInRow=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#70D925")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), .4, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F1FBE9")]),
        ]))
        elements.append(table)

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(regular, 9)
        canvas.drawRightString(landscape(A4)[0] - cm, cm, f"{canvas.getPageNumber()}. oldal")
        canvas.restoreState()

    doc.build(elements, onFirstPage=footer, onLaterPages=footer)


def _unique_name(base, used, limit):
    base = base[:limit] or "Csoport"
    candidate = base
    number = 2
    while candidate.casefold() in used:
        suffix = f"_{number}"
        candidate = base[:limit-len(suffix)] + suffix
        number += 1
    used.add(candidate.casefold())
    return candidate


def export_report_excel(path, groups_or_items, telephelyek, columns, title=None,
                        subtitle=None, include_waste=True, *, prune_empty=True):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    groups = prepare_report_groups(groups_or_items, telephelyek, columns, include_waste, prune_empty=prune_empty)
    if not groups:
        raise ValueError("Nincs exportálható csoport.")
    workbook = Workbook()
    workbook.remove(workbook.active)
    names = set()
    for group in groups:
        base = re.sub(r"[\\/*?:\[\]\x00-\x1f]", "_", group.name).strip("'") or "Csoport"
        name = _unique_name(base, names, 31)
        sheet = workbook.create_sheet(name)
        thin = Side(border_style="thin", color="888888")
        for row_index, row in enumerate([group.headers] + group.rows, 1):
            for col_index, value in enumerate(row, 1):
                if isinstance(value, str) and len(value) > 32767:
                    raise ValueError("Az Excel-cellák legfeljebb 32767 karaktert tartalmazhatnak.")
                cell = sheet.cell(row=row_index, column=col_index, value=value)
                if isinstance(value, str):
                    # Permit data is text, never an executable spreadsheet formula.
                    cell.data_type = "s"
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if row_index == 1:
                    cell.font = Font(color="FFFFFF", bold=True)
                    cell.fill = PatternFill("solid", fgColor="000000")
                    cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for col_index in range(len(group.columns)):
            values = [group.headers[col_index]] + [row[col_index] for row in group.rows]
            width = max(len(line) for value in values for line in (_render_str(value).splitlines() or [""]))
            sheet.column_dimensions[get_column_letter(col_index+1)].width = min(60, max(12, width+2))
    workbook.save(str(path))
    workbook.close()


def export_report(path, items_or_groups, telephelyek, columns, fmt="PDF", title=None,
                  group_by=None, group_name=None, date_filter=None, logo_path=None,
                  include_waste=True, per_group=False, *, prune_empty=True):
    """Export one file or one file per group; legacy public arguments remain valid."""
    fmt = fmt.upper()
    if fmt not in ("PDF", "XLSX", "EXCEL", "EXCEL (XLSX)"):
        raise ValueError("Nem támogatott exportformátum.")
    groups = items_or_groups if isinstance(items_or_groups, dict) else {"Összes": items_or_groups or []}
    if not groups:
        raise ValueError("Nincs exportálható csoport.")
    exporter = export_report_pdf if fmt == "PDF" else export_report_excel
    kwargs = {"title": title, "include_waste": include_waste, "prune_empty": prune_empty}
    if fmt == "PDF":
        kwargs["logo_path"] = logo_path
    if not per_group:
        exporter(path, groups, telephelyek, columns, **kwargs)
        return
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf" if fmt == "PDF" else ".xlsx"
    used = {p.stem.casefold() for p in directory.iterdir() if p.suffix.lower() == suffix}
    for name, items in groups.items():
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .") or "Csoport"
        # Prefixing avoids Windows reserved device names (CON, AUX, COM1, ...).
        base = "kimutatas_" + base
        filename = _unique_name(base, used, 120) + suffix
        exporter(directory / filename, {name: items}, telephelyek, columns, **kwargs)

from typing import List, Dict, Any, Optional, Union
from pathlib import Path
from datetime import datetime, date, timedelta
import os
import logging
import re
import math
import json

logger = logging.getLogger(__name__)

# ----------------- PDF deps -----------------
try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, PageBreak, Image, Spacer, Paragraph, Frame
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.units import cm
    PDF_ENABLED = True
except Exception:
    PDF_ENABLED = False

# ----------------- Excel deps -----------------
try:
    import pandas as pd
    from openpyxl.utils import get_column_letter
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    EXCEL_ENABLED = True
except Exception:
    EXCEL_ENABLED = False

# ----------------- Default column defs -----------------
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
KEY_TO_HEADER = {k: h for k, h in ALL_COLUMNS}

# ----------------- Helper functions -----------------
def _register_fonts():
    """Register fonts for Hungarian characters."""
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        (str(Path(__file__).parent / "fonts/DejaVuSans.ttf"), str(Path(__file__).parent / "fonts/DejaVuSans-Bold.ttf"))
    ]
    for reg, regb in candidates:
        if os.path.exists(reg) and os.path.exists(regb):
            try:
                pdfmetrics.registerFont(TTFont("App-Regular", reg))
                pdfmetrics.registerFont(TTFont("App-Bold", regb))
                return "App-Regular", "App-Bold"
            except Exception as ex:
                logger.debug("Font reg failed: %s", ex)
    return "Helvetica", "Helvetica-Bold"

REGULAR_FONT, BOLD_FONT = _register_fonts()

def _fmt_date(value: Optional[Union[str, date]], fmt="%Y/%m") -> str:
    # keep behavior: empty string stays empty; only None -> ""
    if value is None:
        return ""
    if value == "":
        return ""
    try:
        if isinstance(value, date):
            return value.strftime(fmt)
        if isinstance(value, str) and "T" in value:
            return datetime.fromisoformat(value).date().strftime(fmt)
        return datetime.strptime(value, "%Y.%m.%d").date().strftime(fmt)
    except Exception:
        # fallback to string representation (but not "None")
        try:
            return str(value)
        except Exception:
            return ""

def _fmt_date_full(value: Optional[Union[str, date]]) -> str:
    """
    Return full original date formatted as YYYY.MM.DD (e.g. 2025.04.20).
    If value is None or empty -> return empty string.
    """
    if value is None:
        return ""
    if value == "":
        return ""
    try:
        if isinstance(value, date):
            return value.strftime("%Y.%m.%d")
        if isinstance(value, str) and "T" in value:
            return datetime.fromisoformat(value).date().strftime("%Y.%m.%d")
        # accept YYYY-MM-DD
        return datetime.strptime(value, "%Y.%m.%d").date().strftime("%Y.%m.%d")
    except Exception:
        try:
            # try to parse more loosely
            d = datetime.fromisoformat(str(value))
            return d.date().strftime("%Y.%m.%d")
        except Exception:
            # fallback to string but avoid "None"
            try:
                return str(value)
            except Exception:
                return ""

def _format_hatya(e: dict) -> str:
    ht = e.get("hataly_tipus")
    if ht is None:
        ht = ""
    try:
        ht = ht.strip().lower()
    except Exception:
        ht = str(ht).lower() if ht is not None else ""
    if ht == "fixed":
        return _fmt_date(e.get("lejarat"), "%Y/%m")
    if ht == "indefinite" or (not ht and e.get("hatarozatlan") ):
        return "Határozatlan"
    return "Változtatásig"

def _none_to_empty(v: Any) -> Any:
    """Return empty string only when v is None; otherwise return v unchanged."""
    return "" if v is None else v

def _render_str(v: Any) -> str:
    """Render value as string for output, but treat None as empty string."""
    if v is None:
        return ""
    # keep empty string as-is
    if isinstance(v, str):
        return v
    try:
        return str(v)
    except Exception:
        return ""

def _extract_review_entries(e: dict) -> List[tuple]:
    """
    Shared extractor used by report_builder exporter.
    """
    entries: List[tuple] = []
    raw = e.get("felulvizsgalatok")
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, list):
                for it in parsed:
                    if isinstance(it, dict):
                        d = it.get("date")
                        note = (it.get("note") or "").strip()
                        if d:
                            try:
                                dd = datetime.fromisoformat(d).date()
                                entries.append((dd, note))
                            except Exception:
                                try:
                                    dd = datetime.strptime(str(d), "%Y.%m.%d").date()
                                    entries.append((dd, note))
                                except Exception:
                                    continue
        except Exception:
            entries = []

    if not entries:
        fv_single = e.get("felulvizsgalat_date")
        if fv_single:
            try:
                dd = datetime.fromisoformat(fv_single).date()
                entries.append((dd, ""))
            except Exception:
                try:
                    dd = datetime.strptime(str(fv_single), "%Y.%m.%d").date()
                    entries.append((dd, ""))
                except Exception:
                    pass

    if not entries:
        try:
            fv_nap = int(e.get("felulvizsgalat_nap") or 0)
        except Exception:
            fv_nap = 0
        lejarat = e.get("lejarat")
        if fv_nap > 0 and lejarat:
            try:
                ld = datetime.strptime(lejarat, "%Y.%m.%d").date()
                comp = ld - timedelta(days=fv_nap)
                entries.append((comp, f"{fv_nap} nappal a lejárat előtt (számított)"))
            except Exception:
                pass

    if not entries:
        try:
            from utils import compute_review_date
            fv = compute_review_date(e)
            if fv:
                entries.append((fv, ""))
        except Exception:
            pass

    unique = {}
    for dt, note in entries:
        key = dt.isoformat()
        if key not in unique:
            unique[key] = (dt, note or "")
    return [unique[k] for k in sorted(unique.keys())]

def _format_review_dates_only(e: dict, date_format: str = "%Y.%m.%d") -> str:
    entries = _extract_review_entries(e)
    if not entries:
        return ""
    return "\n".join(dt.strftime(date_format) for dt, _ in entries)

def _format_review_block(e: dict, date_format: str = "%Y.%m.%d", for_pdf: bool = False) -> str:
    entries = _extract_review_entries(e)
    if not entries:
        return ""
    if for_pdf:
        lines = []
        for dt, note in entries:
            ds = dt.strftime(date_format)
            if note:
                lines.append(f"<strong>{ds}</strong>: {note}")
            else:
                lines.append(f"<strong>{ds}</strong>")
        return "FELÜLVIZSGÁLATOK:<br/>" + "<br/>".join(lines)
    else:
        lines = []
        for dt, note in entries:
            ds = dt.strftime(date_format)
            if note:
                lines.append(f"{ds} : {note}")
            else:
                lines.append(f"{ds}")
        return "FELÜLVIZSGÁLATOK:\n" + "\n".join(lines)

def _prepare_row(e: dict, tp_map: Dict[Any,str], columns: List[str], include_waste=True) -> Dict[str,Any]:
    """
    Prepare row values. Important: keep non-None values unchanged where possible;
    only replace None with empty string when rendering (_render_str).
    Special handling:
     - 'modosito_hat': if contains ';' then replace with newline for proper wrapping.
     - 'lejarat'/'lejarat_display': always show the original full date (YYYY.MM.DD) if present.
     - 'felulvizsgalat': return dates only (multiline).
     - 'megjegyzes': append FELÜLVIZSGÁLATOK block (multiline). For PDF the caller will convert '\n' to '<br/>'.
    """
    row = {}
    for key in columns:
        if key == "telephely":
            val = tp_map.get(e.get("telephely_id"))
            row[key] = _none_to_empty(val)
        elif key == "kategoria":
            row[key] = _none_to_empty(e.get("kategoria"))
        elif key == "engedely_nev":
            row[key] = _none_to_empty(e.get("engedely_nev"))
        elif key == "kiado_hatosag":
            row[key] = _none_to_empty(e.get("kiado_hatosag"))
        elif key == "hatarozatszam":
            v = e.get("hatarozatszam")
            if v is None:
                row[key] = ""
            else:
                # preserve value but normalize newlines
                row[key] = (v if isinstance(v, str) else str(v)).replace("\n", " ")
        elif key == "modosito_hat":
            v = e.get("modosito_hat")
            if v is None:
                row[key] = ""
            else:
                # If semicolon separated, convert semicolons to line breaks (user requested)
                s = v if isinstance(v, str) else str(v)
                # Normalize '; ' and ';' to newline
                s = re.sub(r";\s*", "\n", s)
                row[key] = s
        elif key in ("lejarat","lejarat_display"):
            # Per request: always show original full date in cells (YYYY.MM.DD) if available
            row[key] = _fmt_date_full(e.get("lejarat"))
        elif key == "megjegyzes":
            mg = _none_to_empty(e.get("megjegyzes"))
            # append review block (plain text)
            review_block = _format_review_block(e, "%Y.%m.%d", for_pdf=False)
            if review_block:
                if mg:
                    mg = mg + "\n\n" + review_block
                else:
                    mg = review_block
            # include waste compact info if present
            if (e.get("kategoria")=="Hulladékgazdálkodási engedély") and include_waste:
                parts=[]
                for k2 in ("hulladek_nv_elokez","hulladek_v_elokez","hulladek_nv_gyujt","hulladek_v_gyujt"):
                    val = e.get(k2)
                    if val is not None and val != "":
                        parts.append(f"{KEY_TO_HEADER.get(k2,k2)}: {_render_str(val)}")
                if parts:
                    mg = (mg + "\n" if mg else "") + "[" + ", ".join(parts) + "]"
            row[key] = mg
        elif key == "felulvizsgalat":
            # dates only, multiline
            row[key] = _format_review_dates_only(e, "%Y.%m.%d")
        else:
            row[key] = _none_to_empty(e.get(key))
    return row

def _prune_empty_columns(items: List[Dict[str, Any]], columns: List[str], tp_map: Dict[Any,str], include_waste: bool) -> List[str]:
    """
    Remove columns that are empty for ALL items in the group. Preserve some essential columns.
    Only considers None/empty-string as empty.
    """
    if not items:
        return [c for c in columns]
    essential = {"telephely", "kategoria", "engedely_nev"}
    result = []
    for col in columns:
        if col in essential:
            result.append(col)
            continue
        has_value = False
        for e in items:
            v = _prepare_row(e, tp_map, [col], include_waste).get(col)
            if v not in (None, ""):
                has_value = True
                break
        if has_value:
            result.append(col)
    return result

# (a fájl többi része változatlan, a PDF útvonalon a _format_review_block(..., for_pdf=True) használható a megjegyzés HTML előállításához)
def build_header_context(
    *,
    group_by: Optional[str]=None,
    group_name: Optional[str]=None,
    telephely_map: Optional[Dict]=None,
    date_filter: Optional[Dict]=None
) -> str:
    parts=[]
    if group_by and group_name and group_name!="Összes":
        if group_by=="telephely":
            resolved = str(group_name)
            if telephely_map:
                if group_name in telephely_map:
                    resolved = telephely_map[group_name]
                else:
                    try:
                        gi = int(group_name)
                        if gi in telephely_map:
                            resolved = telephely_map[gi]
                    except Exception:
                        for k,v in telephely_map.items():
                            if str(v) == str(group_name):
                                resolved = v
                                break
            parts.append(f"{resolved}")
        elif group_by=="kategoria":
            parts.append(f"{group_name}")
        elif group_by in ("lejarat_month","lejarat","lejarat_year"):
            try:
                m = re.match(r"^(\d{4})[-/](\d{2})", str(group_name))
                if m:
                    y, mm = m.group(1), m.group(2)
                    parts.append(f"{y}/{mm}")
                else:
                    parts.append(f"{group_name}")
            except Exception:
                parts.append(f"{group_name}")

    if date_filter and date_filter.get("enabled"):
        fr = date_filter.get("from")
        to = date_filter.get("to")
        fr_s = fr.strftime("%Y.%m.%d") if fr else "–"
        to_s = to.strftime("%Y.%m.%d") if to else "–"
        parts.append(f"Dátum: {fr_s}—{to_s}")

    if not parts: return ""
    return "("+"; ".join(parts)+")"

# ----------------- PDF export -----------------
def _pdf_footer(canvas, doc):
    canvas.saveState()
    try:
        canvas.setFont(REGULAR_FONT,9)
    except Exception:
        canvas.setFont("Helvetica",9)
    canvas.drawRightString(landscape(A4)[0]-1*cm,1*cm,f"{canvas.getPageNumber()}. oldal")
    canvas.restoreState()

def export_report_pdf(
    path: str,
    groups_or_items: Union[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]],
    telephelyek: List[dict],
    columns: Union[List[str], Dict[str, List[str]]],
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    logo_path: Optional[str] = None,
    include_waste: bool = True,
) -> None:
    """
    Unified PDF exporter.
    - groups_or_items: either dict group_name->items or list of items (treated as single group "Összes")
    - columns: either global list of keys or dict mapping group_name->list(keys)
    - For each group the exporter prunes columns that are empty for that group's items (dynamic headers).
    Only None values are converted to empty strings; other values are preserved and stringified when rendered.
    Note: subtitle is intentionally ignored (no alcím).
    """
    if not PDF_ENABLED:
        print(f"PDF export disabled, would write to {path}")
        return

    tp_map = {t["id"]: t.get("nev","Ismeretlen") for t in telephelyek}
    groups = groups_or_items if isinstance(groups_or_items, dict) else {"Összes": groups_or_items or []}

    doc = SimpleDocTemplate(path, pagesize=landscape(A4),
                            leftMargin=0.6*cm, rightMargin=0.6*cm, topMargin=0.8*cm, bottomMargin=1.6*cm)
    styles = {
        "title": ParagraphStyle("title", fontName=BOLD_FONT, fontSize=16, spaceAfter=6, alignment=1),
        "h": ParagraphStyle("h", fontName=BOLD_FONT, fontSize=9, alignment=1),
        "c": ParagraphStyle("c", fontName=REGULAR_FONT, fontSize=9, leading=11),
        "info": ParagraphStyle("info", fontName=REGULAR_FONT, fontSize=10, leading=11, alignment=1)
    }
    elements = []

    # logo only if provided and title exists (no subtitle rendering)
    if logo_path and os.path.exists(logo_path) and (title):
        try:
            img = Image(logo_path, width=6*cm, height=2*cm, kind='proportional')
            img.hAlign = 'CENTER'
            elements.append(img)
            elements.append(Spacer(1,0.2*cm))
        except Exception:
            logger.debug("Logo add failed", exc_info=True)

    if title:
        elements.append(Paragraph(_render_str(title), styles["title"]))
    # subtitle intentionally not appended
    if title:
        elements.append(Spacer(1,0.2*cm))

    # iterate groups
    grp_items = list(groups.items())
    for idx, (gname, items) in enumerate(grp_items):
        items_sorted = sorted(items, key=lambda e: (e.get("kategoria") or "").lower())
        # resolve columns for this group
        if isinstance(columns, dict):
            group_cols = columns.get(gname, columns.get("default", list(KEY_TO_HEADER.keys())))
        else:
            group_cols = columns or list(KEY_TO_HEADER.keys())
        # prune empty columns for group (dynamic headers)
        effective_cols = _prune_empty_columns(items_sorted, group_cols, tp_map, include_waste)

        # build table header and rows
        headers = [Paragraph(KEY_TO_HEADER.get(k, k), styles["h"]) for k in effective_cols]
        table_data = [headers]
        # prepare text strings and compute max length per column for adaptive width
        col_texts = {k: [KEY_TO_HEADER.get(k, k)] for k in effective_cols}
        for e in items_sorted:
            r = _prepare_row(e, tp_map, effective_cols, include_waste)
            row_cells = []
            for k in effective_cols:
                val = r.get(k, "")
                cell_text = _render_str(val)
                col_texts[k].append(cell_text)
                row_cells.append(Paragraph(cell_text.replace("\n","<br/>"), styles["c"]))
            table_data.append(row_cells)

        page_w = landscape(A4)[0] - doc.leftMargin - doc.rightMargin

        # compute base weights and scale them by content length so wider content gets more space
        def _col_base_weight(k):
            if k in ("engedely_nev","megjegyzes"):
                return 3.0
            if k in ("telephely","kategoria"):
                return 1.0
            if k in ("hatarozatszam","kiado_hatosag"):
                return 1.2
            if k in ("lejarat","lejarat_display"):
                # dates need less width usually
                return 0.9
            return 0.9

        # compute a length factor per column (based on max number of characters)
        weights = []
        for k in effective_cols:
            base = _col_base_weight(k)
            texts = col_texts.get(k, [""])
            max_len = max((len(t) for t in texts), default=0)
            # length factor: 1 + log(1 + max_len) / 3  -> gentle growth for long content
            length_factor = 1.0 + (math.log1p(max_len) / 3.0) if max_len > 0 else 1.0
            weights.append(base * length_factor)

        total_w = sum(weights) if weights else 1.0
        col_widths = [page_w * (w / total_w) for w in weights]

        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),HexColor("#70D925")),
            ('TEXTCOLOR',(0,0),(-1,0),colors.black),
            ('ALIGN',(0,0),(-1,-1),'LEFT'),
            ('VALIGN',(0,0),(-1,-1),'TOP'),
            ('GRID',(0,0),(-1,-1),0.4,colors.grey),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,HexColor("#F1FBE9")])
        ]))

        # group header (title line per group)
        grp_context = build_header_context(group_by=None, group_name=gname, telephely_map=tp_map, date_filter=None)
        if grp_context:
            grp_title = Paragraph(f"<b>{_render_str(gname)}</b> {grp_context}", styles["info"])
            elements.append(grp_title)
            elements.append(Spacer(1,0.15*cm))
        else:
            elements.append(Paragraph(f"<b>{_render_str(gname)}</b>", styles["info"]))
            elements.append(Spacer(1,0.15*cm))

        elements.append(t)

        if idx < len(grp_items) - 1:
            elements.append(PageBreak())

    doc.build(elements, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)

# ----------------- Excel export -----------------
def export_report_excel(
    path: str,
    groups_or_items: Union[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]],
    telephelyek: List[dict],
    columns: Union[List[str], Dict[str, List[str]]],
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    include_waste: bool = True,
) -> None:
    """
    Excel exporter:
    - If groups_or_items is dict -> each group becomes a sheet (or single sheet per export when single group)
    - columns may be dict mapping group->list or a global list
    - Column pruning same as PDF: hide columns that are empty for a group's data
    Only None values are turned into empty strings in the sheet; other values preserved/stringified.
    Note: subtitle is ignored (no alcím).
    """
    if not EXCEL_ENABLED:
        print(f"Excel export disabled, would write to {path}")
        return

    tp_map = {t["id"]: t.get("nev","Ismeretlen") for t in telephelyek}
    groups = groups_or_items if isinstance(groups_or_items, dict) else {"Összes": groups_or_items or []}

    # prepare writer
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for gname, items in groups.items():
            if not items:
                # create empty sheet with requested headers
                cols = (columns if isinstance(columns, list) else (columns.get(gname, list(KEY_TO_HEADER.keys())) if isinstance(columns, dict) else list(KEY_TO_HEADER.keys())))
                df = pd.DataFrame(columns=[KEY_TO_HEADER.get(k,k) for k in cols])
                sheet_name = (_render_str(gname)[:31]) if gname else "Üres"
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                continue

            # pick columns for this group
            if isinstance(columns, dict):
                group_cols = columns.get(gname, columns.get("default", list(KEY_TO_HEADER.keys())))
            else:
                group_cols = columns or list(KEY_TO_HEADER.keys())

            effective_cols = _prune_empty_columns(items, group_cols, tp_map, include_waste)

            rows = []
            for e in items:
                r = _prepare_row(e, tp_map, effective_cols, include_waste)
                # ensure only None -> "", others preserved then stringified by pandas if needed
                rows.append([_none_to_empty(r.get(k, "")) for k in effective_cols])

            df = pd.DataFrame(rows, columns=[KEY_TO_HEADER.get(k,k) for k in effective_cols])
            sheet_name = (_render_str(gname)[:31]) if gname else "Sheet1"
            df.to_excel(writer, sheet_name=sheet_name, index=False)

            # styling: header + enable wrap_text for all data cells (so '\n' from modosito_hat shows)
            ws = writer.sheets[sheet_name]
            thin = Side(border_style="thin", color="888888")
            # header row style
            for col_idx, _ in enumerate(effective_cols, start=1):
                cell = ws.cell(row=1, column=col_idx)
                cell.font = Font(color="FFFFFF", bold=True)
                cell.fill = PatternFill(start_color="000000", end_color="000000", fill_type="solid")
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            # data cells: enable wrap_text so semicolon->newline conversion displays nicely
            for row_idx in range(2, 2 + len(rows)):
                for col_idx in range(1, len(effective_cols) + 1):
                    cell = ws.cell(row=row_idx, column=col_idx)
                    # preserve existing value, but ensure wrap_text True
                    cell.alignment = Alignment(wrap_text=True, vertical="top")

            # autosize
            for i, col in enumerate(ws.columns, start=1):
                max_len = 0
                for cell in col:
                    v = cell.value
                    # treat None as empty string for measuring
                    if v is None:
                        v = ""
                    else:
                        v = str(v)
                    # consider multi-line; take the longest line
                    longest_line = max((len(line) for line in v.splitlines()), default=0)
                    max_len = max(max_len, longest_line)
                ws.column_dimensions[get_column_letter(i)].width = min(60, max(12, max_len + 2))

# ----------------- Convenience wrapper -----------------
def export_report(
    path: Union[str, Path],
    items_or_groups: Union[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]],
    telephelyek: List[dict],
    columns: Union[List[str], Dict[str, List[str]]],
    fmt: str = "PDF",
    title: Optional[str] = None,
    group_by: Optional[str] = None,
    group_name: Optional[str] = None,
    date_filter: Optional[Dict] = None,
    logo_path: Optional[str] = None,
    include_waste: bool = True,
    per_group: bool = False,
):
    """
    Unified entry point used by the UI.
    - path: file path (if per_group True and fmt is PDF/XLSX, path is treated as directory)
    - items_or_groups: either dict or flat list
    - columns: list or dict
    - per_group: if True and items_or_groups is dict -> create separate files per group inside `path` (treated as dir)
    Behavior change requested: ONLY None values are replaced with empty strings ("") in outputs.
    Subtitle (alcím) is ignored and not shown.
    """
    if isinstance(path, Path):
        path = path.as_posix()

    # flatten or preserve groups
    groups = items_or_groups if isinstance(items_or_groups, dict) else {"Összes": items_or_groups or []}

    if fmt.upper().startswith("PDF"):
        if per_group and isinstance(groups, dict):
            # require path to be a directory
            out_dir = path
            os.makedirs(out_dir, exist_ok=True)
            for gname, items in groups.items():
                safe = re.sub(r"[^A-Za-z0-9_\-\.]", "_", str(gname)).strip("_") or "group"
                out_path = os.path.join(out_dir, f"{safe}.pdf")
                # columns may be dict or list: pass through
                try:
                    export_report_pdf(out_path, {gname: items}, telephelyek, columns, title=_render_str(title), subtitle=None, logo_path=logo_path, include_waste=include_waste)
                except Exception as exc:
                    logger.exception("PDF export per-group failed for %s: %s", gname, exc)
                    raise
        else:
            # single file containing all groups (separate pages)
            export_report_pdf(path, groups, telephelyek, columns, title=_render_str(title), subtitle=None, logo_path=logo_path, include_waste=include_waste)
    else:
        # Excel
        if per_group and isinstance(groups, dict):
            out_dir = path
            os.makedirs(out_dir, exist_ok=True)
            for gname, items in groups.items():
                safe = re.sub(r"[^A-Za-z0-9_\-\.]", "_", str(gname)).strip("_") or "group"
                out_path = os.path.join(out_dir, f"{safe}.xlsx")
                try:
                    export_report_excel(out_path, {gname: items}, telephelyek, columns, title=_render_str(title), subtitle=None, include_waste=include_waste)
                except Exception as exc:
                    logger.exception("Excel export per-group failed for %s: %s", gname, exc)
                    raise
        else:
            # single workbook with a sheet per group
            export_report_excel(path, groups, telephelyek, columns, title=_render_str(title), subtitle=None, include_waste=include_waste)
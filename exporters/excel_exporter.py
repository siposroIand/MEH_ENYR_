"""
Advanced Excel exporter for MEH Engedelynyilvantarto.

- options supports:
    include_archived: bool
    date_format: str
    group_by: "telephely"|"kategoria"|None
    include_waste: bool
    category_order: list[str]
    columns: list of column keys to include (subset of DEFAULT_COLUMNS keys)
    telephely_ids: list of allowed telephely ids (if present, only those telephelyek records are exported)
    categories: list of allowed categories (if present)
    prefilter: callable(items)->items  (optional)
"""
import re
import pandas as pd
from datetime import datetime
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from config.app_config import get_ordered_categories
from utils.reviews import review_entries as _extract_review_entries

# Full column set: mapping keys used internally -> (header text, row_key)
ALL_COLUMNS: List[Tuple[str, str]] = [
    ("telephely", "Telephely"),
    ("kategoria", "Engedélytípus"),
    ("engedely_nev", "Tárgy"),
    ("kiado_hatosag", "Kiadó hatóság"),
    ("hatarozatszam", "Határozat száma"),
    ("modosito_hat", "Módosító határozat"),
    ("letrejott", "Létrejött"),
    ("lejarat_display", "Hatálya"),
    ("felulvizsgalat", "Felülvizsgálat"),
    ("responsible_name", "Felelős"),
    ("megjegyzes", "Megjegyzés"),
    ("hulladek_nv_elokez", "NV-Előkezelés"),
    ("hulladek_v_elokez", "V-Előkezelés"),
    ("hulladek_nv_gyujt", "NV-Gyűjtés"),
    ("hulladek_v_gyujt", "V-Gyűjtés"),
]

# default order: telephely first, then category etc.
DEFAULT_COLUMN_KEYS = [k for k, _ in ALL_COLUMNS]


def _format_hatya(e: dict, out_date_format: str = "%Y.%m.%d") -> str:
    """
    Format the 'lejarat' (expiration) display consistently as YYYY.MM.DD when fixed.
    Other cases keep previous textual labels.
    """
    ht = (e.get("hataly_tipus") or "").strip().lower()
    if ht == "fixed":
        lej = e.get("lejarat")
        if not lej:
            return ""
        # try common parsers
        ld = None
        try:
            ld = datetime.fromisoformat(lej).date()
        except Exception:
            try:
                ld = datetime.strptime(str(lej), "%Y.%m.%d").date()
            except Exception:
                try:
                    ld = datetime.strptime(str(lej), "%Y-%m-%d").date()
                except Exception:
                    ld = None
        if ld:
            return ld.strftime(out_date_format)
        return str(lej)
    if ht == "indefinite" or (not ht and e.get("hatarozatlan")):
        return "Határozatlan"
    return "Változtatásig"


def _format_review_dates_only(e: dict, date_format: str = "%Y.%m.%d") -> str:
    """
    Return dates as multiline string (one per line), formatted by date_format.
    Each line includes the optional note after a colon:
      YYYY.MM.DD: note
    If note is empty, only the date is returned.
    """
    entries = _extract_review_entries(e)
    if not entries:
        return ""
    lines = []
    for dt, note in entries:
        ds = dt.strftime(date_format)
        if note:
            lines.append(f"{ds}: {note}")
        else:
            lines.append(ds)
    return "\n".join(lines)


def _format_review_block_for_megjy(e: dict, date_format: str = "%Y.%m.%d") -> str:
    """
    (Kept for compatibility but NOT appended to megjegyzes anymore by default.)
    Return the block to append to the 'megjegyzes' field:
    FELÜLVIZSGÁLATOK:
    YYYY.MM.DD : note
    YYYY.MM.DD : note
    """
    entries = _extract_review_entries(e)
    if not entries:
        return ""
    lines = []
    for dt, note in entries:
        ds = dt.strftime(date_format)
        if note:
            lines.append(f"{ds} : {note}")
        else:
            lines.append(f"{ds}")
    return "FELÜLVIZSGÁLATOK:\n" + "\n".join(lines)


def _normalize_multiline(value: Optional[str]) -> str:
    r"""
    Normalize various separators into single newline-separated lines.
    Preserves existing line order, strips whitespace, removes empty lines.
    Separators normalized: newline, comma, semicolon, pipe.
    Important:
      - forward-slash '/' is NOT treated as a separator (it is preserved).
      - backslash '\' is NOT treated as a separator (it is preserved).
      - if a separator is escaped (e.g. "\,"), the escape is removed and the separator kept in the text.
    """
    if not value:
        return ""
    if not isinstance(value, str):
        value = str(value)
    # Split on actual newlines OR on unescaped separators: , ; |
    parts = re.split(r'\r?\n|(?<!\\)[,;|]+', value)
    cleaned = []
    for p in parts:
        if not p:
            continue
        s = p.strip()
        if not s:
            continue
        # unescape separators if they were escaped (e.g. "\," -> ",")
        s = re.sub(r'\\([,;|])', r'\1', s)
        cleaned.append(s)
    return "\n".join(cleaned)


def _prepare_row(e: dict, tp_map: dict, date_format: str, include_waste: bool) -> Dict[str, str]:
    row = {}
    tp = tp_map.get(e.get("telephely_id")) or "Ismeretlen"
    row["telephely"] = tp
    row["kategoria"] = e.get("kategoria") or ""
    row["engedely_nev"] = _normalize_multiline(e.get("engedely_nev") or "")
    row["kiado_hatosag"] = _normalize_multiline(e.get("kiado_hatosag") or "")
    # hatarozatszam: ensure each number on its own line
    row["hatarozatszam"] = _normalize_multiline(e.get("hatarozatszam") or "")
    row["modosito_hat"] = _normalize_multiline(e.get("modosito_hat") or "")
    row["letrejott"] = e.get("letrejott") or ""
    # lejarat_display: always format fixed dates as YYYY.MM.DD
    row["lejarat_display"] = _format_hatya(e, out_date_format="%Y.%m.%d")
    # Felülvizsgálat: csak a dátumok egymás alatt, külön oszlopban (most note-okkal kiegészítve)
    row["felulvizsgalat"] = _format_review_dates_only(e, date_format) or ""
    row["responsible_name"] = e.get("responsible_name") or ""
    # Megjegyzés: csak az eredeti megjegyzés (nem fűzünk ide felülvizsgálati blokkot)
    row["megjegyzes"] = e.get("megjegyzes") or ""
    if include_waste:
        row["hulladek_nv_elokez"] = e.get("hulladek_nv_elokez") or ""
        row["hulladek_v_elokez"] = e.get("hulladek_v_elokez") or ""
        row["hulladek_nv_gyujt"] = e.get("hulladek_nv_gyujt") or ""
        row["hulladek_v_gyujt"] = e.get("hulladek_v_gyujt") or ""
    else:
        row["hulladek_nv_elokez"] = ""
        row["hulladek_v_elokez"] = ""
        row["hulladek_nv_gyujt"] = ""
        row["hulladek_v_gyujt"] = ""
    return row


def export_to_excel(path: str, items: List[dict], telephelyek: List[dict], options: Optional[Dict] = None):
    opts = options or {}
    if not items:
        raise ValueError("No items to export")

    tp_map = {t["id"]: t.get("nev", "Ismeretlen") for t in telephelyek}
    date_format = opts.get("date_format", "%Y.%m.%d")
    include_waste = opts.get("include_waste", True)
    group_by = opts.get("group_by", "telephely")
    include_archived = opts.get("include_archived", False)

    # columns selection: list of internal keys in default order
    req_columns: Optional[List[str]] = opts.get("columns")
    if req_columns:
        # validate keys
        columns = [k for k in req_columns if k in DEFAULT_COLUMN_KEYS]
        if not columns:
            columns = DEFAULT_COLUMN_KEYS
    else:
        columns = DEFAULT_COLUMN_KEYS

    # telephely/category filters
    allowed_tp_ids = set(opts.get("telephely_ids") or [])
    allowed_cats = set(opts.get("categories") or [])

    # optional prefilter callable gets precedence
    prefilter = opts.get("prefilter")

    # apply simple prefilters
    def _apply_prefilters(src_items: List[dict]) -> List[dict]:
        out = []
        for e in src_items:
            if not include_archived and e.get("archivalt"):
                continue
            if allowed_tp_ids and (e.get("telephely_id") not in allowed_tp_ids):
                continue
            if allowed_cats and (e.get("kategoria") not in allowed_cats):
                continue
            out.append(e)
        if callable(prefilter):
            try:
                return list(prefilter(out))
            except Exception:
                return out
        return out

    items = _apply_prefilters(items)
    if not items:
        raise ValueError("No items to export")

    # prepare rows
    prepared = [_prepare_row(e, tp_map, date_format, include_waste) for e in items]

    # grouping
    groups = {}
    if group_by == "kategoria":
        for p, e in zip(prepared, items):
            key = e.get("kategoria") or "Ismeretlen"
            groups.setdefault(key, []).append(p)
    elif group_by == "telephely":
        for p in prepared:
            key = p.get("telephely") or "Ismeretlen"
            groups.setdefault(key, []).append(p)
    else:
        groups["Összes"] = prepared

    # order groups using category_order if requested
    category_order = opts.get("category_order") or get_ordered_categories()
    keys = list(groups.keys())
    if any(k in category_order for k in keys):
        ordered_keys = [k for k in category_order if k in groups] + [k for k in keys if k not in category_order]
    else:
        ordered_keys = sorted(keys)

    headers = []
    # map columns -> human header names using ALL_COLUMNS
    header_map = {k: h for k, h in ALL_COLUMNS}
    headers = [header_map.get(k, k) for k in columns]

    # write workbook
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        all_rows = []
        for name in ordered_keys:
            grp = groups.get(name, [])
            # ensure consistent order of columns
            rows_out = [[row.get(col_key, "") for col_key in columns] for row in grp]
            df = pd.DataFrame(rows_out, columns=headers)
            safe_sheet = (name[:31] or "Sheet")
            df.to_excel(writer, sheet_name=safe_sheet, index=False)
            all_rows.extend(rows_out)
        if all_rows:
            df_all = pd.DataFrame(all_rows, columns=headers)
            df_all.to_excel(writer, sheet_name="Összes", index=False)

        # styling: simple header style and autosize + wrap data cells + alternating row fill
        wb = writer.book
        thin = Side(border_style="thin", color="C0C0C0")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        # subtle header fill for letisztult megjelenés
        header_fill = PatternFill(start_color="000000", end_color="000000", fill_type="solid")
        header_font = Font(color="ffffff", bold=True)
        # alternating row fill
        row_fill_even = PatternFill(start_color="EBEAEA", end_color="EBEAEA", fill_type="solid")
        row_fill_odd = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

        # Name of the felulvizsgalat header (localized)
        fv_header_name = header_map.get("felulvizsgalat", "Felülvizsgálat")

        for ws in wb.worksheets:
            # style header and compute column widths
            max_col_widths = {}
            # find index of felulvizsgalat column (after header row written)
            fv_col_idx = None
            for col_idx, col in enumerate(ws.columns, start=1):
                # Header cell
                try:
                    header_cell = ws.cell(row=1, column=col_idx)
                    header_cell.fill = header_fill
                    header_cell.font = header_font
                    header_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    header_cell.border = border
                    # detect felulvizsgalat column
                    if str(header_cell.value or "").strip() == fv_header_name:
                        fv_col_idx = col_idx
                except Exception:
                    pass

                max_len = 0
                # Data cells: enable wrap_text and top alignment to preserve DB line breaks
                for row_idx, c in enumerate(col, start=1):
                    try:
                        if isinstance(c.value, str):
                            # Permit/profile text must never become an Excel formula.
                            c.data_type = "s"
                            c.hyperlink = None
                        v = str(c.value) if c.value is not None else ""
                        if len(v) > max_len:
                            max_len = len(v)
                        # apply style to data cells (skip header row which already styled)
                        if row_idx > 1:
                            c.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
                            c.border = border
                    except Exception:
                        pass
                try:
                    width = min(60, max(12, max_len + 2))
                    ws.column_dimensions[get_column_letter(col_idx)].width = width
                    max_col_widths[col_idx] = width
                except Exception:
                    pass

            # alternating row fill for readability (apply after header)
            try:
                for r in range(2, ws.max_row + 1):
                    fill = row_fill_even if (r % 2 == 0) else row_fill_odd
                    for c in ws[r]:
                        try:
                            c.fill = fill
                        except Exception:
                            pass
            except Exception:
                pass

            # Rich-text: bold the date part in the Felülvizsgálat column, leave trailing note plain.
            if fv_col_idx is not None:
                try:
                    # regex matches lines starting with YYYY.MM.DD optionally followed by ": " and the rest
                    line_re = re.compile(r'^(\d{4}\.\d{2}\.\d{2})(:?\s*)(.*)$')
                    for r in range(2, ws.max_row + 1):
                        cell = ws.cell(row=r, column=fv_col_idx)
                        v = cell.value
                        if not v:
                            continue
                        lines = str(v).splitlines()
                        rt = CellRichText()
                        for i, line in enumerate(lines):
                            m = line_re.match(line)
                            if m:
                                datepart, sep, rest = m.group(1), m.group(2) or "", m.group(3) or ""
                                rt.append(TextBlock(InlineFont(b=True), datepart))
                                if sep or rest:
                                    rt.append(sep + rest)
                            else:
                                # fallback: no date recognized, keep as normal text
                                rt.append(line)
                            if i != len(lines) - 1:
                                rt.append("\n")
                        # assign rich text object back to cell
                        cell.value = rt
                        cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
                        cell.border = border
                except Exception:
                    # if anything fails here, we silently continue with plain text (no crash)
                    pass

            try:
                ws.freeze_panes = "A2"
                last_col = get_column_letter(ws.max_column)
                ws.auto_filter.ref = f"A1:{last_col}{ws.max_row}"
            except Exception:
                pass
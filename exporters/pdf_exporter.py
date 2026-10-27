# exporters/pdf_exporter.py
"""
Advanced PDF exporter for MEH Engedelynyilvantarto.

Features:
 - options dict driven (date_format, include_archived, group_by, include_logo, category_order)
 - moves felülvizsgálat info into the megjegyzés column as a labeled badge block
 - dates formatted as YYYY.MM.DD
 - Badge flowable draws rounded pill labels (colored background + white bold text)
 - megjegyzés cell layout: megjegyzés (ha van) / badge (centered) / dátumok (soronként) — vertikálisan
"""
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, PageBreak, Image, Spacer, Paragraph, Flowable
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.units import cm
import os
from datetime import datetime
from typing import Optional, Dict, List
from xml.sax.saxutils import escape

from config.app_config import get_ordered_categories
from utils.reviews import review_entries as _extract_review_entries


def footer_only(canvas, doc):
    canvas.saveState()
    try:
        registered = pdfmetrics.getRegisteredFontNames()
    except Exception:
        registered = []
    font_name = 'Arial-Magyar' if 'Arial-Magyar' in registered else 'Helvetica'
    canvas.setFont(font_name, 9)
    canvas.drawRightString(landscape(A4)[0] - 1*cm, 1*cm, f"{canvas.getPageNumber()}. oldal")
    canvas.restoreState()


def _format_hatya(e: dict) -> str:
    """
    Hatály / lejárat megjelenítése YYYY.MM.DD formátumban, vagy 'Határozatlan' / 'Változtatásig'.
    """
    ht = (e.get("hataly_tipus") or "").strip().lower()
    if ht == "fixed":
        val = e.get("lejarat")
        if not val:
            return ""
        try:
            dd = datetime.fromisoformat(val).date()
            return dd.strftime("%Y.%m.%d")
        except Exception:
            try:
                dd = datetime.strptime(str(val), "%Y-%m-%d").date()
                return dd.strftime("%Y.%m.%d")
            except Exception:
                return str(val)
    if ht == "indefinite" or (not ht and e.get("hatarozatlan")):
        return "Határozatlan"
    return "Változtatásig"


# ----------------- Badge Flowable -----------------
class Badge(Flowable):
    """
    Draws a rounded 'pill' label with background color and centered white text.
    width: computed from text if not provided (simple approx).
    height: fixed (in points).
    """
    def __init__(self, text: str, bgcolor=HexColor("#70D925"), textColor=colors.white,
                 fontName: str = "Helvetica-Bold", fontSize: int = 8, padding_x: float = 6, padding_y: float = 3, radius: float = 6):
        super().__init__()
        self.text = str(text)
        self.bgcolor = bgcolor
        self.textColor = textColor
        self.fontName = fontName
        self.fontSize = fontSize
        self.padding_x = padding_x
        self.padding_y = padding_y
        self.radius = radius  # corner radius in points
        self._width = None
        self._height = None

    def wrap(self, availWidth, availHeight):
        # approximate text width using canvas stringWidth
        try:
            sw = pdfmetrics.stringWidth(self.text, self.fontName, self.fontSize)
        except Exception:
            sw = len(self.text) * (self.fontSize * 0.55)
        w = sw + 2 * self.padding_x
        h = self.fontSize + 2 * self.padding_y
        self._width = w
        self._height = h
        return (w, h)

    def draw(self):
        c = self.canv
        w = self._width or 50
        h = self._height or (self.fontSize + 4)
        r = min(self.radius, h / 2, w / 2)
        c.saveState()
        try:
            c.setFillColor(self.bgcolor)
            c.setStrokeColor(self.bgcolor)
            c.roundRect(0, 0, w, h, r, stroke=0, fill=1)
        except Exception:
            c.setFillColor(self.bgcolor)
            c.rect(0, 0, w, h, stroke=0, fill=1)
        c.setFillColor(self.textColor)
        try:
            c.setFont(self.fontName, self.fontSize)
        except Exception:
            c.setFont("Helvetica-Bold", self.fontSize)
        text_x = w / 2.0
        text_y = (h - self.fontSize) / 2.0 + (self.fontSize * 0.15)
        c.drawCentredString(text_x, text_y, self.text)
        c.restoreState()


def _review_block_html(e: dict, date_format: str = "%Y.%m.%d") -> str:
    """
    Build HTML block where each line is "<strong>YYYY.MM.DD</strong>: note" or "<strong>YYYY.MM.DD</strong>".
    """
    entries = _extract_review_entries(e)
    if not entries:
        return ""
    lines = []
    for dt, note in entries:
        ds = dt.strftime(date_format)
        if note:
            lines.append(f"<strong>{ds}</strong>: {escape(note)}")
        else:
            lines.append(f"<strong>{ds}</strong>")
    return "<br/>".join(lines)


def export_groups_pdf(path: str, groups: Dict[str, List[dict]], telephely_map: Dict[str, str],
                      mode: str = "telephely", logo_path: Optional[str] = None,
                      telephely_meta: Optional[Dict] = None, custom_title: Optional[str] = None,
                      options: Optional[Dict] = None):
    """
    groups: mapping group_name -> list of entity dicts
    telephely_map: id -> name
    options supports:
      - include_logo: bool
      - category_order: list[str]
      - prefilter: callable(items)->items
    """
    opts = options or {}
    date_format = "%Y.%m.%d"  # fixed per request
    include_logo = opts.get("include_logo", True)
    category_order = opts.get("category_order") or get_ordered_categories()

    if not path:
        raise ValueError("No output path")

    # register fonts when possible (Windows common)
    font_p = "C:/Windows/Fonts/arial.ttf"
    font_b = "C:/Windows/Fonts/arialbd.ttf"
    if os.path.exists(font_p) and os.path.exists(font_b):
        try:
            pdfmetrics.registerFont(TTFont('Arial-Magyar', font_p))
            pdfmetrics.registerFont(TTFont('Arial-Bold-Magyar', font_b))
            base_f, bold_f = 'Arial-Magyar', 'Arial-Bold-Magyar'
        except Exception:
            base_f, bold_f = 'Helvetica', 'Helvetica-Bold'
    else:
        base_f, bold_f = 'Helvetica', 'Helvetica-Bold'

    doc = SimpleDocTemplate(path, pagesize=landscape(A4),
                            leftMargin=0.6*cm, rightMargin=0.6*cm, topMargin=0.6*cm, bottomMargin=1.5*cm)

    styles = {
        "title": ParagraphStyle("title", fontName=bold_f, fontSize=16, spaceAfter=6),
        "h": ParagraphStyle("h", fontName=bold_f, fontSize=9, alignment=1),
        "c": ParagraphStyle("c", fontName=base_f, fontSize=9, leading=12),
        "info": ParagraphStyle("info", fontName=base_f, fontSize=10, leading=11)
    }

    prefilter = opts.get("prefilter")

    keys = list(groups.keys())
    if any(k in category_order for k in keys):
        ordered_keys = [k for k in category_order if k in groups] + [k for k in keys if k not in category_order]
    else:
        ordered_keys = sorted(keys)

    elements = []

    # compute printable width once
    page_w = landscape(A4)[0] - doc.leftMargin - doc.rightMargin

    for i, key in enumerate(ordered_keys):
        items = groups.get(key, []) or []
        if callable(prefilter):
            items = prefilter(items)

        if custom_title:
            title_par = Paragraph(escape(custom_title), styles["title"])
        else:
            if mode == "telephely":
                title_par = Paragraph(f"MÉH Zrt. – {key.upper()} TELEPHELYÉNEK ENGEDÉLYEI", styles["title"])
            elif mode == "nezet":
                title_par = Paragraph(f"MÉH Zrt. – ENGEDÉLYEK NYILVÁNTARTÁSA (KIVONAT)", styles["title"])
            else:
                title_par = Paragraph(f"MÉH Zrt. – {key} nyilvántartásai", styles["title"])

        info_par = None
        if mode == "telephely" and telephely_meta and key in telephely_meta:
            meta = telephely_meta.get(key, {})
            parts = []
            if meta.get("cim"): parts.append(f"<strong>Cím:</strong> {meta.get('cim')}")
            if meta.get("ktj"): parts.append(f"<strong>KTJ:</strong> {meta.get('ktj')}")
            if parts:
                info_par = Paragraph(" &nbsp;&nbsp; ".join(parts), styles["info"])

        if include_logo and logo_path and os.path.exists(logo_path):
            try:
                img = Image(logo_path, width=3.6*cm, height=1.2*cm, kind='proportional')
            except Exception:
                img = None
            if img:
                header_table = Table([[ [title_par, info_par] if info_par else [title_par], img ]], colWidths=[page_w - (3.8*cm), 3.8*cm])
                header_table.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP'), ('ALIGN', (1,0), (1,0), 'RIGHT')]))
                elements.append(header_table)
            else:
                elements.append(title_par)
                if info_par: elements.append(info_par)
        else:
            elements.append(title_par)
            if info_par: elements.append(info_par)

        elements.append(Spacer(1, 0.25*cm))

        # Table header: removed Felülvizsg. column
        header = [
            Paragraph("Engedélytípus" if mode == "telephely" else "Telephely", styles["h"]),
            Paragraph("Tárgy", styles["h"]),
            Paragraph("Kibocsátó hatóság", styles["h"]),
            Paragraph("Határozat száma", styles["h"]),
            Paragraph("Hatályos", styles["h"]),
            Paragraph("Megjegyzés", styles["h"]),
            Paragraph("Felelős", styles["h"]),
        ]
        table_data = [header]

        def _sort_key(e):
            if mode == "telephely":
                return (e.get("kategoria") or "").lower()
            else:
                return (telephely_map.get(e.get("telephely_id"), "") or "").lower()

        items_sorted = sorted(items, key=_sort_key)
        # desired widths for main table columns (before scaling)
        desired_col_w = [4.0*cm, 6.0*cm, 4.5*cm, 4.5*cm, 2.5*cm, 10.0*cm, 3.0*cm]
        total_desired = sum(desired_col_w)
        if total_desired > page_w:
            scale = page_w / total_desired
            main_col_w = [w * scale for w in desired_col_w]
        else:
            main_col_w = desired_col_w

        for e in items_sorted:
            tp_name = telephely_map.get(e.get("telephely_id"), "") if telephely_map else (e.get("telephely_id") or "")
            hataly_display = _format_hatya(e)

            fv_block_html = _review_block_html(e, date_format)
            base_megj = escape(e.get("megjegyzes", "") or "")
            if fv_block_html:
                # create Badge with slightly larger font/padding for visual parity
                b = Badge("FELÜLVIZSGÁLAT(OK):", bgcolor=HexColor("#70D925"), textColor=colors.black,
                          fontName=bold_f, fontSize=6, padding_x=6, padding_y=3, radius=8)
                # create content html - replace newline chars with <br/>
                if base_megj:
                    content_html = f"{base_megj.replace(chr(10), '<br/>')}"
                else:
                    content_html = ""
                dates_html = fv_block_html  # contains <strong>YYYY.MM.DD</strong>: note lines joined with <br/>
                # Build a vertical nested table: [megjegyzes], [Spacer], [badge centered], [Spacer], [dates paragraph]
                # Use content column width equal to main_col_w[5]
                content_col_w = main_col_w[5]
                nested_data = []
                if content_html:
                    nested_data.append([Paragraph(content_html, styles["c"])])
                    nested_data.append([Spacer(1, 4)])
                # badge row
                nested_data.append([b])
                nested_data.append([Spacer(1, 4)])
                nested_data.append([Paragraph(dates_html, styles["c"])])
                nested = Table(nested_data, colWidths=[content_col_w], hAlign='LEFT')

                # badge index számítása: ha van base_megj (előtte van Paragraph és Spacer), akkor a badge sor indexe 2, különben 0
                if content_html:
                    badge_row = 2
                else:
                    badge_row = 0

                nested.setStyle(TableStyle([
                    ('ALIGN', (0, badge_row), (0, badge_row), 'LEFT'),   # badge balra igazítása
                    ('VALIGN', (0,0), (-1,-1), 'TOP'),
                    ('LEFTPADDING', (0,0), (-1,-1), 6),
                    ('RIGHTPADDING', (0,0), (-1,-1), 6),
                    ('LEFTPADDING', (0, badge_row), (0, badge_row), 2),  # kisebb bal-padding csak a badge-nél
                    ('TOPPADDING', (0,0), (-1,-1), 0),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 0),
                ]))
                megj_cell = nested
            else:
                megj_cell = Paragraph((base_megj or ""), styles["c"])

            if e.get("kategoria") == "Hulladékgazdálkodási engedély":
                parts = []
                for k, lbl in [("hulladek_nv_elokez", "NV-Elő"), ("hulladek_v_elokez", "V-Elő"),
                               ("hulladek_nv_gyujt", "NV-Gyűjt"), ("hulladek_v_gyujt", "V-Gyűjt")]:
                    if e.get(k):
                        parts.append(f"{lbl}: {escape(str(e[k]))}")
                if parts:
                    extra = "[" + ", ".join(parts) + "]"
                    if isinstance(megj_cell, Paragraph):
                        text_html = (base_megj.replace("\n", "<br/>") + "<br/>" + extra) if base_megj else extra
                        megj_cell = Paragraph(text_html, styles["c"])
                    else:
                        megj_cell = Table([[megj_cell], [Paragraph(extra, styles["c"])]],
                                          colWidths=[main_col_w[5]], hAlign="LEFT")

            hat_text = escape(e.get("hatarozatszam") or "").replace("\n", " ")
            mh = (e.get("modosito_hat") or "").strip()
            if mh:
                parts = [p.strip() for p in mh.split(";") if p.strip()]
                for p in parts:
                    hat_text += "<br/><u><strong>mód.:</strong></u> " + escape(p)

            table_data.append([
                Paragraph(escape((e.get("kategoria") if mode == "telephely" else tp_name) or ""), styles["c"]),
                Paragraph(escape(e.get("engedely_nev", "") or ""), styles["c"]),
                Paragraph(escape(e.get("kiado_hatosag", "") or ""), styles["c"]),
                Paragraph(hat_text or "", styles["c"]),
                Paragraph(hataly_display or "", styles["c"]),
                megj_cell,
                Paragraph(escape(e.get("responsible_name") or ""), styles["c"]),
            ])

        t = Table(table_data, colWidths=main_col_w, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), HexColor("#70D925")),
            ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, HexColor("#F1FBE9")])
        ]))
        elements.append(t)

        if i < len(ordered_keys) - 1:
            elements.append(PageBreak())

    doc.build(elements, onFirstPage=footer_only, onLaterPages=footer_only)
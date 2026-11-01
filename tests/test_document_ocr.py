from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.document_ocr import (
    DocumentFields, DocumentReadCancelled, DocumentReadError,
    ExtractionRule, OcrRules, extract_fields, merge_fields, read_document,
)


class FieldExtractionTests(unittest.TestCase):
    def test_subject_and_numeric_dates(self):
        for label in ("Hatályos", "Érvényes", "HATALYOS", "ervenyes"):
            for value in ("2031. 02. 28.", "2031-02-28", "2031/02/28"):
                with self.subTest(label=label, value=value):
                    fields = extract_fields(f"Tárgy: Hulladékgazdálkodási engedély\n{label}: {value}")
                    self.assertEqual(fields.subject, "Hulladékgazdálkodási engedély")
                    self.assertEqual(fields.expiry, date(2031, 2, 28))
                    self.assertEqual(fields.validity, "fixed")

    def test_multiline_subject_stops_at_next_field(self):
        fields = extract_fields(
            "Tárgy:\nHulladékgazdálkodási\nengedély módosítása\n"
            "Ügyiratszám: 123\nÉrvényes:\n2030. december 31.\n\nIndokolás\n2026.01.01."
        )
        self.assertEqual(fields.subject, "Hulladékgazdálkodási engedély módosítása")
        self.assertEqual(fields.expiry, date(2030, 12, 31))

    def test_intervals_use_end_not_start(self):
        for value in (
            "2025.01.01. – 2030.12.31.",
            "2025-01-01 - 2030-12-31",
            "2025. január 1-től 2030. december 31-ig",
            "2025. január 1-től - 2030. december 31-ig",
        ):
            with self.subTest(value=value):
                self.assertEqual(extract_fields(f"Hatályos: {value}").expiry, date(2030, 12, 31))

    def test_invalid_unrelated_or_ambiguous_dates_not_applied(self):
        for text in (
            "Kelt: 2030.01.01.",
            "Hatályos: 2030.02.30.",
            "Érvényes: 2028.01.01-től",
            "Hatályos: 2030.01.01. vagy 2031.01.01.",
            "Hatályos: 2030.01.01. - 2029.01.01.",
            "Hatályos: 2030.01.01.\nÉrvényes: 2031.01.01.",
            "Hatályos: a kiadástól számított öt év",
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_fields(text).validity)

    def test_non_fixed_validity(self):
        for text, expected in (
            ("határozatlan ideig", "indefinite"),
            ("visszavonásig", "until_modified"),
            ("változtatásig", "until_modified"),
        ):
            fields = extract_fields(f"Hatályos: {text}")
            self.assertEqual(fields.validity, expected)
            self.assertIsNone(fields.expiry)

    def test_multiple_files_conflict_without_arbitrary_overwrite(self):
        first = extract_fields("Tárgy: Első\nÉrvényes: 2030.01.01.")
        second = extract_fields("Tárgy: Második\nÉrvényes: 2031.01.01.")
        fields = merge_fields([first, second])
        self.assertIsNone(fields.subject)
        self.assertIsNone(fields.validity)
        self.assertEqual(len(fields.warnings), 2)
        self.assertEqual(merge_fields([first, first]), first)

    def test_empty_and_missing_labels(self):
        self.assertEqual(extract_fields(""), DocumentFields())
        self.assertEqual(extract_fields("Dokumentum 2030.01.01."), DocumentFields())
        self.assertIsNone(extract_fields("Tárgy:\nÉrvényes: 2030.01.01.").subject)


class DocumentReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_image(self):
        from PIL import Image, ImageDraw, ImageFont
        import reportlab
        font = ImageFont.truetype(str(Path(reportlab.__file__).parent / "fonts/Vera.ttf"), 44)
        image = Image.new("RGB", (1700, 450), "white")
        draw = ImageDraw.Draw(image)
        draw.text((60, 90), "Tárgy: Hulladékgazdálkodási engedély", font=font, fill="black")
        draw.text((60, 210), "Érvényes: 2030. december 31.", font=font, fill="black")
        return image

    def test_native_pdf_without_tesseract(self):
        import reportlab
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen.canvas import Canvas
        pdfmetrics.registerFont(TTFont("OCRTest", str(Path(reportlab.__file__).parent / "fonts/Vera.ttf")))
        path = self.root / "native.pdf"
        canvas = Canvas(str(path))
        canvas.setFont("OCRTest", 16)
        canvas.drawString(30, 780, "Tárgy: Hulladékgazdálkodási engedély")
        canvas.drawString(30, 750, "Hatályos: 2030.12.31.")
        canvas.showPage()
        canvas.drawString(30, 780, "Second page, unrelated text.")
        canvas.save()
        with patch("pytesseract.image_to_string", side_effect=AssertionError("Native PDF needs no OCR")):
            fields = read_document(path)
        self.assertEqual(fields.subject, "Hulladékgazdálkodási engedély")
        self.assertEqual(fields.expiry, date(2030, 12, 31))

    def test_real_ocr_image_and_scanned_pdf(self):
        image = self.make_image()
        self.addCleanup(image.close)
        for extension in ("png", "pdf", "tiff"):
            with self.subTest(extension=extension):
                path = self.root / f"scan.{extension}"
                image.save(path)
                fields = read_document(path)
                self.assertEqual(fields.subject, "Hulladékgazdálkodási engedély")
                self.assertEqual(fields.expiry, date(2030, 12, 31))

    def test_cancellation(self):
        with self.assertRaises(DocumentReadCancelled):
            read_document(self.root / "file.pdf", lambda: True)

    def test_corrupt_document(self):
        path = self.root / "corrupt.pdf"
        path.write_bytes(b"not a pdf")
        with self.assertRaises(DocumentReadError):
            read_document(path)

    def test_missing_engine_and_timeout_are_explicit(self):
        import pytesseract
        image = self.make_image()
        self.addCleanup(image.close)
        path = self.root / "scan.png"
        image.save(path)
        for error in (pytesseract.TesseractNotFoundError(), RuntimeError("OCR timeout")):
            with self.subTest(error=error):
                with patch("pytesseract.image_to_string", side_effect=error):
                    with self.assertRaises(DocumentReadError):
                        read_document(path)

    def test_custom_rules_reach_image_reader(self):
        image = self.make_image()
        self.addCleanup(image.close)
        path = self.root / "rules.png"
        image.save(path)
        rules = OcrRules(ExtractionRule("Kezdet", "Vége"), ExtractionRule("Lejárat", "Zárás"))
        with patch("pytesseract.image_to_string", return_value="Kezdet próba Vége Lejárat 2030.12.31. Zárás"):
            fields = read_document(path, rules=rules)
        self.assertEqual(fields.subject, "próba")
        self.assertEqual(fields.expiry, date(2030, 12, 31))


class CustomRuleTests(unittest.TestCase):
    def test_after_marker_and_between_markers(self):
        rules = OcrRules(ExtractionRule("Tárgy"), ExtractionRule("Érvényes", "lezárás"))
        fields = extract_fields("TÁRGY: Hulladék engedély\n\nÉrvényes 2030.12.31. lezárás", rules)
        self.assertEqual(fields.subject, "Hulladék engedély")
        self.assertEqual(fields.expiry, date(2030, 12, 31))

    def test_literals_whitespace_accents_and_multiline(self):
        rules = OcrRules(subject=ExtractionRule("A tárgy [kezdete]:", "(vége)"))
        fields = extract_fields("a  tárgy\n[kezdete]:Első\nmásodik (vége)", rules)
        self.assertEqual(fields.subject, "Első második")

    def test_configured_rule_does_not_fallback(self):
        rules = OcrRules(subject=ExtractionRule("Kezdet", "Vége"))
        fields = extract_fields("Tárgy: Beépített\nÉrvényes: 2030.01.01.", rules)
        self.assertIsNone(fields.subject)
        self.assertEqual(fields.expiry, date(2030, 1, 1))
        self.assertTrue(fields.warnings)

    def test_missing_end_and_conflicting_values_are_not_applied(self):
        for text in ("Kezdet érték", "Kezdet egy Vége\nKezdet kettő Vége", "Kezdet egy Vége\nKezdet kettő"):
            fields = extract_fields(text, OcrRules(subject=ExtractionRule("Kezdet", "Vége")))
            self.assertIsNone(fields.subject)
            self.assertTrue(fields.warnings)

    def test_end_marker_does_not_cross_another_start(self):
        fields = extract_fields(
            "Kezdet hibás\nKezdet jó Vége", OcrRules(subject=ExtractionRule("Kezdet", "Vége")),
        )
        self.assertIsNone(fields.subject)
        self.assertTrue(fields.warnings)

    def test_start_does_not_match_inside_word(self):
        fields = extract_fields("Érvényesség: 2030.01.01.", OcrRules(validity=ExtractionRule("Érvényes")))
        self.assertIsNone(fields.validity)

    def test_validity_rules_preserve_conservative_parsing(self):
        rules = OcrRules(validity=ExtractionRule("Lejárat", "Vége"))
        for value in ("2030.02.30.", "2028.01.01-től", "öt év", "2030.01.01. vagy 2031.01.01."):
            with self.subTest(value=value):
                fields = extract_fields(f"Lejárat {value} Vége", rules)
                self.assertIsNone(fields.validity)
                self.assertTrue(fields.warnings)
        self.assertEqual(extract_fields("Lejárat határozatlan Vége", rules).validity, "indefinite")

    def test_default_warnings_for_overridden_field_are_not_kept(self):
        rules = OcrRules(validity=ExtractionRule("Lejárat", "Vége"))
        fields = extract_fields("Érvényes: hibás\nLejárat 2030.12.31. Vége", rules)
        self.assertEqual(fields.expiry, date(2030, 12, 31))
        self.assertEqual(fields.warnings, ())

    def test_rejects_end_without_start(self):
        with self.assertRaises(ValueError):
            ExtractionRule(end="Vége")

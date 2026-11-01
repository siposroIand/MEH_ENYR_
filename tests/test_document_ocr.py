from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.document_ocr import (
    DocumentFields, DocumentReadCancelled, DocumentReadError,
    extract_fields, merge_fields, read_document,
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

    def test_all_five_fields_and_common_aliases(self):
        fields = extract_fields(
            "Kiadó hatóság: Pest Vármegyei Kormányhivatal\n"
            "Környezetvédelmi Főosztály\n1050 Budapest, Példa utca\n"
            "Ügyiratszám: PE/KTFO/12-3/2026\n"
            "Tárgy: Hulladékkezelési\nengedély\n"
            "Határozat kelte: Budapest, 2026. november 1.\n"
            "Lejárat dátuma: 2030.12.31."
        )
        self.assertEqual(fields.authority, "Pest Vármegyei Kormányhivatal Környezetvédelmi Főosztály")
        self.assertEqual(fields.decision_number, "PE/KTFO/12-3/2026")
        self.assertEqual(fields.subject, "Hulladékkezelési engedély")
        self.assertEqual(fields.issued_on, date(2026, 11, 1))
        self.assertEqual(fields.expiry, date(2030, 12, 31))

    def test_header_authority_closing_date_and_explicit_validity_sentence(self):
        fields = extract_fields(
            "PEST VÁRMEGYEI KORMÁNYHIVATAL\n"
            "KÖRNYEZETVÉDELMI FŐOSZTÁLY\n1050 Budapest\n"
            "Iktatószám: PE/12/2026\nTárgy: Engedély\n"
            "Az engedély 2030. december 31. napjáig érvényes.\n\n"
            "Budapest, 2026. november 1."
        )
        self.assertEqual(fields.authority, "PEST VÁRMEGYEI KORMÁNYHIVATAL KÖRNYEZETVÉDELMI FŐOSZTÁLY")
        self.assertEqual(fields.subject, "Engedély")
        self.assertEqual(fields.issued_on, date(2026, 11, 1))
        self.assertEqual(fields.expiry, date(2030, 12, 31))
        self.assertEqual(
            extract_fields("PEST VÁRMEGYEI\nKORMÁNYHIVATAL\nTárgy: Engedély").authority,
            "PEST VÁRMEGYEI KORMÁNYHIVATAL",
        )
        self.assertEqual(
            extract_fields("Kiadó hatóság: Pest Vármegyei\nKormányhivatal\nTárgy: Engedély").authority,
            "Pest Vármegyei Kormányhivatal",
        )

    def test_applicant_references_and_review_dates_are_not_guessed(self):
        fields = extract_fields(
            "Kérelmező: Minta Kft.\nPest Vármegyei Kormányhivatal\n\n"
            "HATÁROZAT\nKorábbi határozat száma: OLD/2020\n"
            "Felülvizsgálat: 2029.01.01.\n"
            "Az engedély felülvizsgálata 2029.01.01-ig érvényes.\n"
            "Érvényesség kezdete: 2026.01.01.\n"
        )
        self.assertIsNone(fields.authority)
        self.assertIsNone(fields.decision_number)
        self.assertIsNone(fields.issued_on)
        self.assertIsNone(fields.validity)
        for value in ("nem határozatlan", "határozatlan vagy visszavonásig", "2030.01.01. napjától",
                      "kezdete 2026.01.01.", "a jogerőtől számított öt év"):
            with self.subTest(value=value):
                self.assertIsNone(extract_fields("Hatály: " + value).validity)

    def test_conflicts_stay_blocked_across_pages_files_and_templates(self):
        conflict = extract_fields("Tárgy: Első\n\nTárgy: Második")
        result = merge_fields([conflict, extract_fields("Tárgy: Első")])
        self.assertIsNone(result.subject)
        self.assertIn("subject", result.blocked_fields)
        same = merge_fields([
            extract_fields("Kiadó hatóság: PEST VÁRMEGYEI KORMÁNYHIVATAL"),
            extract_fields("Kiadó hatóság: Pest Vármegyei Kormányhivatal"),
        ])
        self.assertIsNotNone(same.authority)
        self.assertEqual(same.warnings, ())
        fields = extract_fields("Kelt: 2031.01.01.\nHatály: 2030.01.01.")
        self.assertIsNone(fields.expiry)
        self.assertIn("validity", fields.blocked_fields)


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
        self.assertIn("Second page, unrelated text.", fields.text)

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

    def test_every_scanned_page_is_read_and_full_text_is_kept(self):
        from PIL import Image
        path = self.root / "all-pages.tiff"
        with Image.new("RGB", (500, 800), "white") as image:
            image.save(path, save_all=True, append_images=[image, image])
        texts = [
            "Kiadó hatóság: Minta Hatóság\nTárgy: Engedély\nIktatószám: TEST/2026",
            "Hatályos: 2030.12.31.\n\nA teljes második oldal szövege.",
            "Kelt: 2026.11.01.\n\nUtolsó oldal szövege.",
        ]
        progress = []
        with patch("pytesseract.image_to_string", side_effect=texts) as recognize:
            fields = read_document(path, progress=lambda page, total: progress.append((page, total)))
        self.assertEqual(recognize.call_count, 3)
        self.assertEqual(progress, [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(fields.issued_on, date(2026, 11, 1))
        self.assertEqual(fields.authority, "Minta Hatóság")
        self.assertEqual(fields.decision_number, "TEST/2026")
        self.assertIn("Utolsó oldal szövege.", fields.text)

    def test_mixed_pdf_is_ocrd_even_when_native_subject_and_expiry_exist(self):
        import reportlab
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen.canvas import Canvas
        pdfmetrics.registerFont(TTFont("MixedOCR", str(Path(reportlab.__file__).parent / "fonts/Vera.ttf")))
        path = self.root / "mixed.pdf"
        canvas = Canvas(str(path))
        canvas.setFont("MixedOCR", 14)
        canvas.drawString(30, 800, "Tárgy: Engedély")
        canvas.drawString(30, 770, "Érvényes: 2030.12.31.")
        with self.make_image() as image:
            canvas.drawImage(ImageReader(image), 30, 100, width=400, height=120)
        canvas.save()
        with patch("pytesseract.image_to_string", return_value="Kiadó hatóság: Minta Hatóság\nKelt: 2026.11.01.") as recognize:
            fields = read_document(path)
        self.assertEqual(recognize.call_count, 1)
        self.assertEqual(fields.subject, "Engedély")
        self.assertEqual(fields.authority, "Minta Hatóság")
        self.assertEqual(fields.issued_on, date(2026, 11, 1))
        self.assertIn("PDF-szöveg", fields.text)
        self.assertIn("OCR:", fields.text)

    def test_failed_page_does_not_prevent_reading_later_pages(self):
        from PIL import Image
        path = self.root / "partial.tiff"
        with Image.new("RGB", (100, 100), "white") as image:
            image.save(path, save_all=True, append_images=[image])
        with patch("pytesseract.image_to_string", side_effect=[RuntimeError("timeout"), "Kelt: 2026.11.01."]):
            fields = read_document(path)
        self.assertEqual(fields.issued_on, date(2026, 11, 1))
        self.assertTrue(any("1. oldal" in warning and "sikertelen" in warning for warning in fields.warnings))
        self.assertIn("2. oldal", fields.text)

    def test_real_full_document_ocr_extracts_five_fields_across_pages(self):
        from PIL import Image, ImageDraw, ImageFont
        import reportlab
        font = ImageFont.truetype(str(Path(reportlab.__file__).parent / "fonts/Vera.ttf"), 44)
        with Image.new("RGB", (2200, 800), "white") as first, Image.new("RGB", (2200, 800), "white") as second:
            draw = ImageDraw.Draw(first)
            for y, text in (
                (60, "Kiadó hatóság: Pest Vármegyei Kormányhivatal"),
                (180, "Ügyiratszám: PE/12-3/2026"),
                (300, "Tárgy: Hulladékgazdálkodási engedély"),
            ):
                draw.text((60, y), text, font=font, fill="black")
            draw = ImageDraw.Draw(second)
            draw.text((60, 60), "Kelt: Budapest, 2026. november 1.", font=font, fill="black")
            draw.text((60, 180), "Az engedély 2030. december 31. napjáig érvényes.", font=font, fill="black")
            for extension in ("pdf", "tiff"):
                with self.subTest(extension=extension):
                    path = self.root / f"whole.{extension}"
                    with first.copy() as first_page, second.copy() as second_page:
                        first_page.save(path, save_all=True, append_images=[second_page])
                    fields = read_document(path)
                    self.assertEqual(fields.authority, "Pest Vármegyei Kormányhivatal")
                    self.assertEqual(fields.decision_number, "PE/12-3/2026")
                    self.assertEqual(fields.subject, "Hulladékgazdálkodási engedély")
                    self.assertEqual(fields.issued_on, date(2026, 11, 1))
                    self.assertEqual(fields.expiry, date(2030, 12, 31))
                    self.assertIn("2. oldal", fields.text)

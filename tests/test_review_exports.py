from contextlib import closing
from datetime import date
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook
import pypdfium2 as pdfium

from core.db import Database
from exporters import excel_exporter, pdf_exporter, report_builder_exporter
from utils.dates import extract_review_entries


class ReviewExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "records.db")
        self.data = {
            "kategoria": "Hulladékgazdálkodási engedély", "hatarozatszam": "ID/123",
            "hataly_tipus": "fixed", "lejarat": "2030-12-31",
            "modositok": ["MOD/1", "MOD/2"], "hulladek_nv_elokez": "1,5 kg",
            "felulvizsgalatok": [
                {"type": "date", "date": "2028-01-02", "note": "Explicit"},
                {"type": "before_expiry", "days": 30, "note": "Relative30"},
                {"type": "before_expiry", "days": 60, "note": "Relative60"},
            ],
        }
        self.db.save_permit(self.data, "permit", "2026-01-01")
        self.record = self.db.get_all_data()["engedelyek"][0]

    def test_every_exporter_resolves_all_reviews_from_normalized_storage(self):
        expected = [(date(2028, 1, 2), "Explicit"), (date(2030, 11, 1), "Relative60"), (date(2030, 12, 1), "Relative30")]
        for exporter in (excel_exporter, pdf_exporter, report_builder_exporter):
            with self.subTest(exporter=exporter.__name__):
                self.assertEqual(exporter._extract_review_entries(self.record), expected)
        self.assertEqual(extract_review_entries({**self.record, "lejarat": "2031-12-31"}), [
            (date(2028, 1, 2), "Explicit"), (date(2031, 11, 1), "Relative60"), (date(2031, 12, 1), "Relative30"),
        ])

    def test_excel_files_contain_all_reviews_modifications_and_waste(self):
        for exporter in ("standard", "report"):
            path = self.root / f"{exporter}.xlsx"
            columns = ["hatarozatszam", "modosito_hat", "felulvizsgalat", "hulladek_nv_elokez"]
            if exporter == "standard":
                excel_exporter.export_to_excel(str(path), [self.record], [], {"columns": columns, "group_by": None})
            else:
                report_builder_exporter.export_report_excel(str(path), {"Group": [self.record]}, [], columns)
            book = load_workbook(path)
            try:
                text = "\n".join(str(cell.value) for sheet in book for row in sheet for cell in row)
                for value in ("2028.01.02", "2030.11.01", "2030.12.01", "MOD/1", "MOD/2", "1,5 kg"):
                    self.assertIn(value, text)
            finally:
                book.close()

    def test_pdf_contains_every_review_date(self):
        path = self.root / "details.pdf"
        pdf_exporter.export_groups_pdf(str(path), {"Group": [self.record]}, {})
        texts = []
        with closing(pdfium.PdfDocument(str(path))) as document:
            for index in range(len(document)):
                with closing(document[index]) as page, closing(page.get_textpage()) as textpage:
                    texts.append(textpage.get_text_range())
        text = "\n".join(texts)
        for value in ("2028.01.02", "2030.11.01", "2030.12.01", "MOD/1", "MOD/2", "1,5 kg"):
            self.assertIn(value, text)

    def test_non_fixed_permits_keep_explicit_but_not_relative_dates(self):
        for validity in ("indefinite", "until_modified"):
            self.assertEqual(extract_review_entries({**self.record, "hataly_tipus": validity}), [
                (date(2028, 1, 2), "Explicit"),
            ])
        self.assertEqual(extract_review_entries({**self.record, "lejarat": None}), [
            (date(2028, 1, 2), "Explicit"),
        ])

    def test_empty_list_is_authoritative_and_legacy_dates_are_supported(self):
        self.assertEqual(extract_review_entries({
            "felulvizsgalatok": "[]", "felulvizsgalat_date": "2028-01-02",
            "felulvizsgalat_nap": 30, "lejarat": "2030-12-31",
        }), [])
        self.assertEqual(extract_review_entries({"felulvizsgalat_date": "2028.01.02"}), [(date(2028, 1, 2), "")])

    def test_coincident_dates_keep_both_notes(self):
        self.assertEqual(extract_review_entries({
            "felulvizsgalatok": [{"date": "2030-12-01", "note": "First"}, {"date": "2030-12-01", "note": "Second"}],
        }), [(date(2030, 12, 1), "First; Second")])

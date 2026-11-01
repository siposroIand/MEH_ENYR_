from datetime import date
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.document_ocr import DocumentReadCancelled, DocumentReadError, extract_region_fields, read_document
from core.ocr_templates import (
    OcrRegion, OcrTemplate, TemplateStore, document_page_count, render_document_page,
)


class FileTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test-artifacts" / str(uuid4())
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)

    def template(self, *regions):
        return OcrTemplate.create("Hulladékengedély 2026", "Pest Vármegye", regions)


class TemplateModelTests(FileTestCase):
    def test_validation_and_normalized_crop(self):
        for rect in ((-0.1, 0, 1, 1), (0, 0, 2, 1), (0.5, 0, 0.2, 1),
                     (0, 0, 0, 1), (0, float("nan"), 1, 1), (0, 0, 1)):
            with self.subTest(rect=rect), self.assertRaises(ValueError):
                OcrRegion("subject", 1, rect)
        for page in (0, -1, True, 1.5):
            with self.subTest(page=page), self.assertRaises(ValueError):
                OcrRegion("subject", page, (0, 0, 1, 1))
        region = OcrRegion("subject", 1, (0.1, 0.2, 0.6, 0.8))
        self.assertEqual(region.pixel_box(1000, 500), (100, 100, 600, 400))
        with self.assertRaises(ValueError):
            self.template(region, region)
        with self.assertRaises(ValueError):
            OcrTemplate("../outside", "Név", "Hatóság", (region,))
        with self.assertRaises(ValueError):
            OcrTemplate.create(" ", "Hatóság", [region])

    def test_persistence_update_delete_and_corrupt_file_isolation(self):
        store = TemplateStore(self.root / "profiles")
        self.assertEqual(store.load(), [])
        first = self.template(OcrRegion("subject", 2, (0.1, 0.2, 0.8, 0.3)))
        store.save(first)
        self.assertEqual(TemplateStore(store.directory).load(), [first])
        updated = OcrTemplate(first.id, "Módosított", first.authority, first.regions)
        store.save(updated)
        (store.directory / "broken.json").write_text("{", encoding="utf-8")
        self.assertEqual(store.load(), [updated])
        self.assertEqual(len(store.warnings), 1)
        saved = json.loads((store.directory / f"{first.id}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 1)
        self.assertNotIn("document_path", saved)
        store.delete(updated)
        self.assertEqual(store.load(), [])
        self.assertTrue((store.directory / "broken.json").exists())

    def test_failed_atomic_write_preserves_previous_profile(self):
        store = TemplateStore(self.root)
        original = self.template(OcrRegion("subject", 1, (0, 0, 1, 1)))
        store.save(original)
        changed = OcrTemplate(original.id, "Új név", original.authority, original.regions)
        with patch("core.ocr_templates.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.save(changed)
        self.assertEqual(store.load(), [original])
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_profile_names_are_unique_within_authority(self):
        store = TemplateStore(self.root)
        first = self.template(OcrRegion("subject", 1, (0, 0, 1, 1)))
        store.save(first)
        duplicate = OcrTemplate.create(first.name.upper(), first.authority.upper(), first.regions)
        with self.assertRaises(ValueError):
            store.save(duplicate)
        self.assertEqual(store.load(), [first])


class RegionExtractionTests(unittest.TestCase):
    def test_new_fields_support_labelled_and_value_only_regions(self):
        self.assertEqual(extract_region_fields("authority", "Minta Hatóság").authority, "Minta Hatóság")
        self.assertEqual(extract_region_fields("issued_on", "2026. november 1.").issued_on, date(2026, 11, 1))
        self.assertEqual(extract_region_fields("decision_number", "Ügyiratszám: PE/12/2026").decision_number, "PE/12/2026")
        self.assertIsNone(extract_region_fields("issued_on", "Hatályos: 2030.01.01.").issued_on)

    def test_value_only_and_labelled_fields(self):
        for text in ("Engedély\nmódosítása", "Tárgy: Engedély módosítása"):
            self.assertEqual(extract_region_fields("subject", text).subject, "Engedély módosítása")
        for text in ("2030. december 31.", "Érvényes: 2030.12.31.", "Lejárat: 2030-12-31"):
            self.assertEqual(extract_region_fields("validity", text).expiry, date(2030, 12, 31))
        for value, validity in (("határozatlan ideig", "indefinite"),
                                ("visszavonásig", "until_modified")):
            self.assertEqual(extract_region_fields("validity", value).validity, validity)
        self.assertEqual(
            extract_region_fields("validity", "2025. január 1-től 2030. december 31-ig").expiry,
            date(2030, 12, 31),
        )

    def test_never_infer_ambiguous_expiry_or_unrelated_dates(self):
        for text in ("", "Kelt: 2030.01.01.", "2030.02.30.", "2030.01.01-től",
                     "határozatlan, 2030.01.01.", "nem határozatlan", "határozatlan vagy visszavonásig",
                     "2030.01.01 vagy 2031.01.01", "2030.01.01. keltezés", "5 év",
                     "nincs határozatlan hatály"):
            with self.subTest(text=text):
                self.assertIsNone(extract_region_fields("validity", text).validity)
        for text in ("Tárgy:", "Tárgy: Engedély\nÉrvényes: 2030.01.01.", "x" * 201):
            self.assertIsNone(extract_region_fields("subject", text).subject)


class TemplateReadingTests(FileTestCase):
    def make_image(self, name="page.png", size=(1000, 800)):
        from PIL import Image
        path = self.root / name
        with Image.new("RGB", size, "white") as image:
            image.save(path)
        return path

    def test_full_page_and_selected_crops_are_ocrd(self):
        path = self.make_image()
        template = self.template(
            OcrRegion("subject", 1, (0.1, 0.1, 0.6, 0.3)),
            OcrRegion("validity", 1, (0.2, 0.5, 0.6, 0.6)),
        )
        sizes = []

        def recognize(image, **kwargs):
            sizes.append(image.size)
            if "config" not in kwargs:
                self.assertEqual(kwargs, {"lang": "hun", "timeout": 45})
                return "Kiadó hatóság: Minta Hatóság\nKelt: 2026.11.01."
            self.assertEqual(kwargs, {"lang": "hun", "timeout": 45, "config": "--psm 6"})
            return "Engedély" if len(sizes) == 2 else "2030.12.31."

        with patch("pytesseract.image_to_string", side_effect=recognize):
            fields = read_document(path, template=template)
        self.assertEqual(sizes, [(1000, 800), (500, 160), (400, 80)])
        self.assertEqual(fields.subject, "Engedély")
        self.assertEqual(fields.expiry, date(2030, 12, 31))
        self.assertEqual(fields.authority, "Minta Hatóság")
        self.assertEqual(fields.issued_on, date(2026, 11, 1))

    def test_real_local_ocr_does_not_hide_conflicting_full_page_values(self):
        from PIL import Image, ImageDraw, ImageFont
        import reportlab
        font = ImageFont.truetype(str(Path(reportlab.__file__).parent / "fonts/Vera.ttf"), 44)
        template = self.template(
            OcrRegion("subject", 1, (0.02, 0.15, 0.95, 0.30)),
            OcrRegion("validity", 1, (0.02, 0.48, 0.70, 0.64)),
        )
        with Image.new("RGB", (1700, 600), "white") as image:
            draw = ImageDraw.Draw(image)
            draw.text((60, 5), "Tárgy: Ezt nem szabad átvenni", font=font, fill="black")
            draw.text((60, 100), "Hulladékgazdálkodási engedély", font=font, fill="black")
            draw.text((60, 300), "2030. december 31.", font=font, fill="black")
            draw.text((60, 500), "Érvényes: 2044.01.01.", font=font, fill="black")
            for extension in ("png", "pdf", "tiff"):
                with self.subTest(extension=extension):
                    path = self.root / f"real.{extension}"
                    image.save(path)
                    fields = read_document(path, template=template)
                    self.assertIsNone(fields.subject)
                    self.assertIsNone(fields.expiry)
                    self.assertIn("subject", fields.blocked_fields)
                    self.assertIn("validity", fields.blocked_fields)

    def test_unconfigured_field_is_not_filled_from_crop(self):
        path = self.make_image()
        template = self.template(OcrRegion("validity", 1, (0, 0, 1, 1)))
        with patch("pytesseract.image_to_string", return_value="2030.12.31."):
            fields = read_document(path, template=template)
        self.assertIsNone(fields.subject)

    def test_missing_pages_and_failed_regions_leave_affected_field_unset(self):
        path = self.make_image()
        template = self.template(
            OcrRegion("subject", 1, (0, 0, 1, 0.5)),
            OcrRegion("subject", 2, (0, 0, 1, 0.5)),
            OcrRegion("validity", 1, (0, 0.5, 1, 1)),
        )
        with patch("pytesseract.image_to_string", side_effect=["", "Engedély", "2030.12.31."]):
            fields = read_document(path, template=template)
        self.assertIsNone(fields.subject)
        self.assertEqual(fields.expiry, date(2030, 12, 31))
        self.assertTrue(any("2." in warning for warning in fields.warnings))
        with patch("pytesseract.image_to_string", side_effect=RuntimeError("OCR timeout")):
            with self.assertRaises(DocumentReadError):
                read_document(path, template=template)

    def test_multipage_pdf_and_tiff_read_all_pages_before_requested_region(self):
        from PIL import Image
        for extension in ("tiff", "pdf"):
            with self.subTest(extension=extension):
                path = self.root / f"multi.{extension}"
                with Image.new("RGB", (1000, 800), "white") as first:
                    with Image.new("RGB", (1000, 800), "black") as second:
                        first.save(path, save_all=True, append_images=[second])
                self.assertEqual(document_page_count(path), 2)
                with render_document_page(path, 2) as preview:
                    self.assertEqual(preview.getpixel((20, 20)), (0, 0, 0))
                template = self.template(OcrRegion("subject", 2, (0.1, 0.1, 0.5, 0.3)))

                def recognize(image, **kwargs):
                    if "config" not in kwargs:
                        return "Kiadó hatóság: Minta Hatóság" if image.getpixel((10, 10)) == (255, 255, 255) else ""
                    self.assertEqual(image.getpixel((10, 10)), (0, 0, 0))
                    return "Második oldal"

                with patch("pytesseract.image_to_string", side_effect=recognize) as ocr:
                    fields = read_document(path, template=template)
                self.assertEqual(fields.subject, "Második oldal")
                self.assertEqual(fields.authority, "Minta Hatóság")
                self.assertEqual(ocr.call_count, 3)

    def test_exif_orientation_matches_preview_and_ocr(self):
        from PIL import Image
        path = self.root / "rotated.jpg"
        with Image.new("RGB", (600, 400), "white") as image:
            exif = image.getexif()
            exif[274] = 6
            image.save(path, exif=exif)
        with render_document_page(path, 1) as preview:
            self.assertEqual(preview.size, (400, 600))
        template = self.template(OcrRegion("subject", 1, (0, 0, 0.5, 1)))

        def recognize(image, **kwargs):
            if "config" not in kwargs:
                self.assertEqual(image.size, (400, 600))
                return ""
            self.assertEqual(image.size, (200, 600))
            return "Forgatott oldal"

        with patch("pytesseract.image_to_string", side_effect=recognize):
            self.assertEqual(read_document(path, template=template).subject, "Forgatott oldal")

    def test_conflicting_regions_and_cancellation_do_not_choose_a_value(self):
        from PIL import Image
        path = self.root / "multi.tiff"
        with Image.new("RGB", (100, 100), "white") as image:
            image.save(path, save_all=True, append_images=[image])
        template = self.template(
            OcrRegion("subject", 1, (0, 0, 1, 1)),
            OcrRegion("subject", 2, (0, 0, 1, 1)),
        )
        with patch("pytesseract.image_to_string", side_effect=["", "", "Első", "Második"]):
            self.assertIsNone(read_document(path, template=template).subject)
        stopped = False

        def recognize(image, **kwargs):
            nonlocal stopped
            stopped = True
            return "Nem alkalmazható"

        with patch("pytesseract.image_to_string", side_effect=recognize) as ocr:
            with self.assertRaises(DocumentReadCancelled):
                read_document(path, lambda: stopped, template=template)
        self.assertEqual(ocr.call_count, 1)


class TemplateUiTests(FileTestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_mouse_selection_is_normalized_at_zoom_and_in_reverse(self):
        from PIL import Image
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtTest import QTest
        from gui.ocr_templates import RegionPreview
        preview = RegionPreview()
        self.addCleanup(preview.close)
        preview.resize(600, 500)
        with Image.new("RGB", (1000, 800)) as image:
            preview.set_image(image)
        preview.show()
        self.app.processEvents()
        regions = []
        preview.region_selected.connect(regions.append)
        for factor in (1, 1.3):
            preview.zoom(factor)
            end = preview.mapFromScene(QPointF(100, 80))
            start = preview.mapFromScene(QPointF(500, 400))
            QTest.mousePress(preview.viewport(), Qt.LeftButton, pos=start)
            QTest.mouseMove(preview.viewport(), end)
            QTest.mouseRelease(preview.viewport(), Qt.LeftButton, pos=end)
            for actual, expected in zip(regions[-1], (0.1, 0.1, 0.5, 0.5)):
                self.assertAlmostEqual(actual, expected, delta=0.005)

    def test_editor_saves_reloads_replaces_regions_and_selects_template(self):
        from PIL import Image
        from PySide6.QtWidgets import QDialog
        from gui.ocr_templates import OcrTemplateEditor, OcrTemplateSelector
        path = self.root / "sample.tiff"
        with Image.new("RGB", (400, 600), "white") as image:
            image.save(path, save_all=True, append_images=[image])
        store = TemplateStore(self.root / "profiles")
        editor = OcrTemplateEditor(store, document_paths=[path])
        self.addCleanup(editor.deleteLater)
        editor.name_edit.setText("Minta")
        editor.authority_edit.setText("Hatóság")
        self.assertEqual(editor.page_spin.maximum(), 2)
        editor.page_spin.setValue(2)
        editor._region_selected((0.1, 0.2, 0.8, 0.3))
        editor._region_selected((0.1, 0.2, 0.9, 0.4))
        self.assertEqual(len(editor.regions), 1)
        self.assertEqual(editor.regions[0].page, 2)
        editor._save()
        self.assertEqual(editor.result(), QDialog.Accepted)
        template = store.load()[0]
        self.assertEqual(template.regions[0].rect, (0.1, 0.2, 0.9, 0.4))
        selector = OcrTemplateSelector(store=store)
        self.addCleanup(selector.deleteLater)
        self.assertIsNone(selector.selected_template())
        selector.refresh(template.id)
        self.assertEqual(selector.selected_template(), template)
        reopened = OcrTemplateEditor(store, template, [path])
        self.addCleanup(reopened.deleteLater)
        self.assertEqual(reopened.regions, list(template.regions))
        reopened.region_list.setCurrentRow(0)
        self.assertEqual(reopened.page_spin.value(), 2)

    def test_worker_passes_template_and_suppresses_cancelled_results(self):
        from core.document_ocr import DocumentFields
        from gui.ocr_worker import OcrWorker
        template = self.template(OcrRegion("subject", 1, (0, 0, 1, 1)))
        paths = [self.root / "image.png"]
        worker = OcrWorker(paths, template=template)
        received = []
        worker.result_ready.connect(lambda fields, errors: received.append((fields, errors)))
        with patch("gui.ocr_worker.read_document", return_value=DocumentFields("Engedély")) as read:
            worker.run()
        self.assertEqual(read.call_args.kwargs["template"], template)
        self.assertEqual(received[0][0].subject, "Engedély")
        received.clear()
        with patch("gui.ocr_worker.read_document", side_effect=DocumentReadCancelled):
            worker.run()
        self.assertEqual(received, [])

    def test_authority_preference_filter_and_editor_prefill(self):
        from gui.ocr_templates import OcrTemplateEditor, OcrTemplateSelector
        store = TemplateStore(self.root / "profiles")
        region = OcrRegion("subject", 1, (0, 0, 1, 1))
        first = OcrTemplate.create("Első minta", "A Hatóság", [region])
        second = OcrTemplate.create("Második minta", "Z Hatóság", [region])
        store.save(first)
        store.save(second)
        selector = OcrTemplateSelector(store=store, authority=" Z  HATÓSÁG ")
        self.addCleanup(selector.deleteLater)
        self.assertIsNone(selector.selected_template())
        self.assertEqual(selector.combo.itemData(1), second)
        self.assertEqual(selector.combo.count(), 3)
        selector.refresh(first.id)
        selector.set_authority("Z hatóság", filter_profiles=True)
        self.assertIsNone(selector.selected_template())
        self.assertEqual(selector.combo.count(), 2)
        selector.set_authority("Ismeretlen", filter_profiles=True)
        self.assertEqual(selector.combo.count(), 1)
        self.assertIn("még nincs sablon", selector.warning_label.text())
        selector.set_authority("")
        self.assertEqual(selector.combo.count(), 3)
        editor = OcrTemplateEditor(store, authority="Z Hatóság")
        self.addCleanup(editor.deleteLater)
        self.assertEqual(editor.authority_edit.text(), "Z Hatóság")

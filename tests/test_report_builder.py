from datetime import date, datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtWidgets import QApplication, QMessageBox
from openpyxl import load_workbook
import pypdfium2 as pdfium

from core.report_data import build_report_groups, parse_date, validity_state
from core.report_templates import ReportTemplateStore
from exporters.report_builder_exporter import prepare_report_groups, export_report
from gui.report_builder import ReportBuilderDialog, run_report


SITES = [{"id": "s1", "nev": "Budapest"}, {"id": "s2", "nev": "Győr"}]


def permit(identifier="p1", **overrides):
    item = {
        "id": identifier, "telephely_id": "s1", "kategoria": "Hulladékgazdálkodási engedély",
        "engedely_nev": "Őrző & <telep>", "kiado_hatosag": "Hatóság A",
        "hatarozatszam": "ABC/2026", "lejarat": "2030-05-10",
        "hataly_tipus": "fixed", "archivalt": 0, "deleted": 0,
    }
    item.update(overrides)
    return item


def ids(groups):
    return [item["id"] for group in groups.values() for item in group]


class ReportDataTests(unittest.TestCase):
    def test_archive_deleted_flags_and_unknown_category(self):
        items = [permit(), permit("arch", archivalt=1), permit("old", archived=True),
                 permit("deleted", deleted="1"), permit("unknown", kategoria=None)]
        self.assertEqual(ids(build_report_groups(items, SITES, {})), ["p1", "unknown"])
        self.assertEqual(ids(build_report_groups(items, SITES, {"include_archived": True})),
                         ["p1", "arch", "old", "unknown"])
        self.assertEqual(ids(build_report_groups(items, SITES, {"selected_categories": ["Ismeretlen"]})),
                         ["unknown"])
        self.assertEqual(ids(build_report_groups(items, SITES, {"selected_categories": []})), [])

    def test_combined_site_category_authority_text_and_date(self):
        items = [
            permit(), permit("other-authority", kiado_hatosag="Hatóság B"),
            permit("other-site", telephely_id="s2"), permit("other-category", kategoria="Egyéb"),
            permit("wrong-text", engedely_nev="Más tárgy"), permit("too-late", lejarat="2030-05-11"),
        ]
        options = {
            "telephely_ids": ["s1"], "selected_categories": ["Hulladékgazdálkodási engedély"],
            "selected_authorities": ["Hatóság A"], "text_filter": "ŐRZŐ",
            "date_filter": {"enabled": True, "from": date(2030, 5, 10), "to": "2030-05-10"},
        }
        self.assertEqual(ids(build_report_groups(items, SITES, options)), ["p1"])
        self.assertEqual(ids(build_report_groups(items, SITES, {"text_filter": "GYŐR"})), ["other-site"])
        self.assertEqual(ids(build_report_groups(items, SITES, {"selected_authorities": []})), [])

    def test_empty_site_selection_is_explicit(self):
        items = [permit()]
        self.assertEqual(ids(build_report_groups(items, SITES, {"telephely_ids": []})), ["p1"])
        self.assertEqual(ids(build_report_groups(items, SITES, {
            "telephely_ids": [], "site_filter_enabled": True,
        })), [])

    def test_validity_states_and_inclusive_expiry_boundaries(self):
        today = date(2030, 5, 10)
        items = [
            permit("today"), permit("old", lejarat="2030-05-09"),
            permit("thirty", lejarat="2030-06-09"), permit("later", lejarat="2030-06-10"),
            permit("indef", hataly_tipus="indefinite"),
            permit("modified", hataly_tipus="until_modified"),
            permit("unknown", lejarat="not a date"),
        ]
        expected = {
            "expired": ["old"], "expiring": ["today", "thirty"],
            "indefinite": ["indef"], "until_change": ["modified"], "unknown": ["unknown"],
            "active": ["today", "thirty", "later", "indef", "modified"],
        }
        for state, wanted in expected.items():
            with self.subTest(state=state):
                self.assertEqual(ids(build_report_groups(items, SITES, {"validity_state": state}, today)), wanted)
        filtered = build_report_groups(items, SITES, {
            "date_filter": {"enabled": True, "from": "2030-05-10", "to": "2030-05-10"},
        }, today)
        self.assertEqual(ids(filtered), ["today"])

    def test_legacy_dates_and_invalid_ranges(self):
        self.assertEqual(parse_date("2030.05.10"), date(2030, 5, 10))
        self.assertEqual(parse_date("2030-05-10T12:15:00"), date(2030, 5, 10))
        self.assertEqual(parse_date(datetime(2030, 5, 10, 12)), date(2030, 5, 10))
        self.assertEqual(validity_state({"hatarozatlan": 1}), "active")
        with self.assertRaisesRegex(ValueError, "kezdő"):
            build_report_groups([permit()], SITES, {"date_filter": {
                "enabled": True, "from": "2030-05-11", "to": "2030-05-10",
            }})

    def test_group_names_sorted_and_no_spurious_expiry_on_indefinite(self):
        groups = build_report_groups([
            permit("z", kiado_hatosag="Z"), permit("a", kiado_hatosag="A"),
        ], SITES, {"group_by": "kiado_hatosag"})
        self.assertEqual(list(groups), ["A", "Z"])
        groups = build_report_groups([permit(hataly_tipus="indefinite")], SITES, {"group_by": "lejarat_month"})
        self.assertEqual(list(groups), ["Ismeretlen"])


class ReportFileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".report-test-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)


class ReportTemplateTests(ReportFileTests):
    def test_persistent_roundtrip_whitelist_and_delete(self):
        path = self.root / "local" / "reports.sqlite3"
        store = ReportTemplateStore(path)
        options = {
            "columns": ["lejarat", "engedely_nev"], "column_order": ["lejarat", "engedely_nev"],
            "format": "XLSX", "text_filter": "Őrző", "selected_categories": [],
            "date_filter": {"enabled": True, "from": date(2030, 1, 1), "to": date(2030, 12, 31)},
            "selected_items": [permit()], "logo_path": "/private/logo.png",
        }
        store.save("../sablon/Őrző", options)
        reopened = ReportTemplateStore(path)
        saved = reopened.load("../sablon/Őrző")
        self.assertEqual(saved["columns"], options["columns"])
        self.assertEqual(saved["date_filter"]["from"], "2030-01-01")
        self.assertEqual(saved["selected_categories"], [])
        self.assertNotIn("selected_items", saved)
        self.assertNotIn("logo_path", saved)
        self.assertEqual([p.name for p in (self.root / "local").iterdir()], ["reports.sqlite3"])
        reopened.delete("../sablon/Őrző")
        self.assertEqual(store.names(), [])

    def test_overwrite_requires_explicit_permission_and_is_atomic(self):
        store = ReportTemplateStore(self.root / "reports.sqlite3")
        store.save("Sablon", {"title": "eredeti"})
        with self.assertRaises(ValueError):
            store.save("Sablon", {"title": "másik"})
        with self.assertRaises(ValueError):
            store.save("Sablon", {"date_filter": {"enabled": True, "from": "rossz"}}, overwrite=True)
        self.assertEqual(store.load("Sablon")["title"], "eredeti")
        store.save("Sablon", {"title": "új"}, overwrite=True)
        self.assertEqual(store.load("Sablon")["title"], "új")

    def test_malformed_unsupported_templates_and_missing_are_errors(self):
        store = ReportTemplateStore(self.root / "reports.sqlite3")
        store.names()
        for payload in ("not JSON", '{"version": 2}', '{"version": 1, "options": {"columns": null}}'):
            with sqlite3.connect(store.path) as connection:
                connection.execute("INSERT OR REPLACE INTO report_templates VALUES ('broken', ?)", (payload,))
            with self.assertRaises(ValueError):
                store.load("broken")
        with self.assertRaises(ValueError):
            store.load("absent")
        with self.assertRaises(ValueError):
            store.delete("absent")
        with self.assertRaises(ValueError):
            store.save("  ", {})


class ReportExportTests(ReportFileTests):
    def pdf_text(self, path):
        document = pdfium.PdfDocument(str(path))
        try:
            texts = []
            for index in range(len(document)):
                page = document[index]
                text_page = page.get_textpage()
                texts.append(text_page.get_text_range())
                text_page.close()
                page.close()
            return "\n".join(texts)
        finally:
            document.close()

    def test_shared_formatting_includes_validity_review_waste_and_order(self):
        item = permit(
            modosito_hat="A; B", letrejott="2025-01-02", megjegyzes="Megjegyzés",
            hulladek_nv_elokez=0,
            felulvizsgalatok=json.dumps([
                {"type": "date", "date": "2030-04-01", "note": "Ellenőrzés"},
                {"type": "before_expiry", "days": 5, "note": "Előtte"},
            ]),
        )
        columns = ["felulvizsgalat", "megjegyzes", "modosito_hat", "lejarat", "letrejott"]
        group = prepare_report_groups([item], SITES, columns, prune_empty=False)[0]
        self.assertEqual(group.columns, columns)
        row = group.rows[0]
        self.assertEqual(row[0], "2030.04.01\n2030.05.05")
        self.assertIn("Ellenőrzés", row[1])
        self.assertIn("Előtte", row[1])
        self.assertIn("NV-Előkezelés: 0", row[1])
        self.assertEqual(row[2:], ["A\nB", "2030.05.10", "2025.01.02"])
        for kind, expected in (("indefinite", "Határozatlan"), ("until_modified", "Változtatásig")):
            group = prepare_report_groups([permit(hataly_tipus=kind)], SITES, ["lejarat"], prune_empty=False)[0]
            self.assertEqual(group.rows, [[expected]])

    def test_excel_and_pdf_contain_same_full_rows_and_columns(self):
        items = [permit(str(i), engedely_nev=f"Engedély {i:02d}", kategoria="Z" if i % 2 else "A")
                 for i in range(12)]
        columns = ["engedely_nev", "lejarat", "kiado_hatosag", "hulladek_v_gyujt"]
        prepared = prepare_report_groups(items, SITES, columns, prune_empty=False)[0]
        excel = self.root / "report.xlsx"
        pdf = self.root / "report.pdf"
        export_report(excel, items, SITES, columns, fmt="XLSX", prune_empty=False)
        export_report(pdf, items, SITES, columns, fmt="PDF", title="Őrző <jelentés> & adatok", prune_empty=False)
        workbook = load_workbook(excel)
        self.addCleanup(workbook.close)
        rows = list(workbook.active.values)
        self.assertEqual(list(rows[0]), prepared.headers)
        self.assertEqual([list("" if v is None else v for v in row) for row in rows[1:]], prepared.rows)
        text = self.pdf_text(pdf)
        self.assertIn("Őrző <jelentés> & adatok", text)
        positions = [text.index(row[0]) for row in prepared.rows]
        self.assertEqual(positions, sorted(positions))
        for header in prepared.headers:
            self.assertIn(header, text)
        self.assertIn("2030.05.10", text)

    def test_pdf_escapes_markup_and_splits_very_long_rows(self):
        item = permit(engedely_nev="Őrző & <telep>", megjegyzes="Hosszú megjegyzés " * 900)
        path = self.root / "long.pdf"
        export_report(path, [item], SITES, ["engedely_nev", "megjegyzes"], prune_empty=False)
        text = self.pdf_text(path)
        self.assertIn("Őrző & <telep>", text)
        self.assertIn("Hosszú megjegyzés", text)

    def test_sheet_names_file_names_and_formulas_are_safe(self):
        groups = {
            "A/B": [permit(engedely_nev="=1+1")],
            "A:B": [permit("p2", engedely_nev="+sum(1,2)")],
            "a:b": [permit("p3")],
            "X" * 40: [permit("p4")],
            "X" * 39 + "Y": [permit("p5")],
        }
        path = self.root / "names.xlsx"
        export_report(path, groups, SITES, ["engedely_nev"], fmt="XLSX", prune_empty=False)
        workbook = load_workbook(path)
        self.addCleanup(workbook.close)
        self.assertEqual(len(workbook.sheetnames), 5)
        self.assertEqual(len({n.casefold() for n in workbook.sheetnames}), 5)
        self.assertTrue(all(len(n) <= 31 for n in workbook.sheetnames))
        self.assertEqual(workbook.worksheets[0]["A2"].value, "=1+1")
        self.assertEqual(workbook.worksheets[0]["A2"].data_type, "s")
        directory = self.root / "groups"
        for fmt in ("XLSX", "PDF"):
            export_report(directory, groups, SITES, ["engedely_nev"], fmt=fmt, per_group=True, prune_empty=False)
            export_report(directory, groups, SITES, ["engedely_nev"], fmt=fmt, per_group=True, prune_empty=False)
            self.assertEqual(len(list(directory.glob("*." + fmt.lower()))), 10)
        self.assertTrue(all(p.parent == directory for p in directory.iterdir()))

    def test_legacy_pruning_retained_but_explicit_columns_not_pruned(self):
        columns = ["engedely_nev", "hulladek_v_gyujt"]
        self.assertEqual(prepare_report_groups([permit()], SITES, columns)[0].columns, ["engedely_nev"])
        self.assertEqual(prepare_report_groups([permit()], SITES, columns, prune_empty=False)[0].columns, columns)
        with self.assertRaises(ValueError):
            prepare_report_groups([permit()], SITES, [], prune_empty=False)
        with self.assertRaises(ValueError):
            export_report(self.root / "bad", [permit()], SITES, columns, fmt="CSV")

    def test_bad_review_data_is_visible_not_silently_omitted(self):
        with self.assertRaisesRegex(ValueError, "JSON"):
            prepare_report_groups([permit(felulvizsgalatok="broken")], SITES, ["megjegyzes"])

    def test_empty_legacy_reviews_and_relative_conditions_remain_readable(self):
        group = prepare_report_groups([permit(felulvizsgalatok="")], SITES, ["megjegyzes"], prune_empty=False)[0]
        self.assertEqual(group.rows, [[""]])
        group = prepare_report_groups([
            permit(hataly_tipus="indefinite", felulvizsgalatok="null", felulvizsgalat_nap=30),
        ], SITES, ["felulvizsgalat", "megjegyzes"], prune_empty=False)[0]
        self.assertEqual(group.rows, [["", "FELÜLVIZSGÁLATOK:\n30 nappal a lejárat előtt"]])


class ReportDialogTests(ReportFileTests):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, items=None, initial=None):
        result = ReportBuilderDialog(
            data={"engedelyek": items if items is not None else [permit()]},
            telephelyek=SITES, initial=initial,
            template_store=ReportTemplateStore(self.root / "templates.sqlite3"),
        )
        self.addCleanup(result.deleteLater)
        return result

    def test_preview_uses_exact_export_model_and_explicit_limit(self):
        items = [permit(str(i), kategoria="Z" if i % 2 else "A",
                        engedely_nev=f"Engedély {i:02d}", modosito_hat="A; B",
                        felulvizsgalatok='[{"date":"2030-05-01","note":"Felülvizsgálat"}]')
                 for i in range(12)]
        options = {"columns": ["felulvizsgalat", "modosito_hat", "lejarat", "engedely_nev", "megjegyzes"]}
        dialog = self.dialog(items, options)
        groups = build_report_groups(items, SITES, dialog.get_options())
        prepared = prepare_report_groups(groups, SITES, options["columns"], prune_empty=False)[0]
        self.assertEqual(dialog.preview_table.rowCount(), 8)
        self.assertIn("8 / 12", dialog.preview_label.text())
        self.assertIn("Teljes export: 12 sor", dialog.preview_label.text())
        for r, row in enumerate(prepared.rows[:8]):
            for c, value in enumerate(row):
                self.assertEqual(dialog.preview_table.item(r, c).text(), str(value))

    def test_filter_controls_refresh_count_and_empty_columns_block_export(self):
        dialog = self.dialog([permit(), permit("archive", archivalt=1), permit("other", kiado_hatosag="Másik")])
        dialog.chk_include_arch.setChecked(True)
        self.assertEqual(dialog.preview_table.rowCount(), 3)
        dialog.text_filter.setText("másik")
        self.assertEqual(dialog.preview_table.rowCount(), 1)
        dialog.chk_date_filter.setChecked(True)
        self.assertTrue(dialog.date_from.isEnabled())
        dialog.date_from.setDate(date(2031, 1, 1))
        dialog.date_to.setDate(date(2030, 1, 1))
        self.assertIn("kezdő", dialog.preview_label.text())
        self.assertFalse(dialog.btn_finish.isEnabled())
        dialog.chk_date_filter.setChecked(False)
        dialog.btn_select_none.click()
        self.assertIn("oszlopot", dialog.preview_label.text())
        self.assertEqual(dialog.preview_table.columnCount(), 0)

    def test_reorder_buttons_and_drag_model_update_preview(self):
        dialog = self.dialog(initial={"columns": ["engedely_nev", "lejarat", "kiado_hatosag"]})
        dialog.columns_list.setCurrentRow(1)
        dialog._move_column_up()
        self.assertEqual(dialog.get_options()["columns"][:2], ["lejarat", "engedely_nev"])
        self.assertEqual(dialog.preview_table.horizontalHeaderItem(0).text(), "Hatálya")
        moved = dialog.columns_list.model().moveRows(QModelIndex(), 0, 1, QModelIndex(), 3)
        self.assertTrue(moved)
        self.assertEqual(dialog.get_options()["columns"], ["engedely_nev", "kiado_hatosag", "lejarat"])
        self.assertEqual(dialog.preview_table.horizontalHeaderItem(0).text(), "Tárgy")

    def test_template_save_load_delete_restores_every_setting(self):
        options = {
            "title": "Éves jelentés", "columns": ["megjegyzes", "engedely_nev"],
            "format": "XLSX", "group_by": "kiado_hatosag", "per_group": True,
            "include_archived": True, "site_filter_enabled": True, "telephely_ids": ["s1"],
            "selected_categories": ["Hulladékgazdálkodási engedély"],
            "selected_authorities": ["Hatóság A"], "text_filter": "őrző", "validity_state": "fixed",
            "date_filter": {"enabled": True, "from": "2030-01-01", "to": "2030-12-31"},
            "filename_hint": "éves_jelentés", "include_logo": False,
        }
        dialog = self.dialog(initial=options)
        original = dialog.get_options()
        with patch("gui.report_builder.QInputDialog.getText", return_value=("Éves", True)):
            dialog._save_template()
        dialog._apply_options({"title": "Megváltozott"})
        dialog._load_template()
        self.assertEqual(dialog.get_options(), original)
        reopened = self.dialog()
        self.assertEqual(reopened.template_combo.currentText(), "Éves")
        reopened._load_template()
        self.assertEqual(reopened.get_options(), original)
        with patch("gui.report_builder.QMessageBox.question", return_value=QMessageBox.Yes):
            reopened._delete_template()
        self.assertEqual(reopened.template_store.names(), [])
        self.assertFalse(reopened.btn_template_load.isEnabled())

    def test_missing_template_selection_stays_restricted(self):
        dialog = self.dialog(initial={"site_filter_enabled": True, "telephely_ids": ["missing"]})
        self.assertEqual(dialog.get_options()["telephely_ids"], ["missing"])
        self.assertEqual(dialog.preview_table.rowCount(), 0)
        dialog.chk_all_sites.setChecked(True)
        self.assertEqual(dialog.preview_table.rowCount(), 1)

    def test_group_preview_switches_without_changing_data(self):
        dialog = self.dialog([permit("a", kiado_hatosag="A"), permit("b", kiado_hatosag="B")],
                             {"group_by": "kiado_hatosag", "columns": ["kiado_hatosag"]})
        self.assertEqual(dialog.preview_group.count(), 2)
        dialog.preview_group.setCurrentIndex(1)
        self.assertEqual(dialog.preview_table.item(0, 0).text(), "B")
        self.assertIn("Teljes export: 2 sor, 2 csoport", dialog.preview_label.text())

    def test_run_report_uses_filters_logo_and_correct_format(self):
        items = [permit(), permit("archive", archivalt=1), permit("other", kiado_hatosag="Másik")]
        dialog = self.dialog(items, {"selected_authorities": ["Hatóság A"], "include_logo": False})
        options = dialog.get_options()
        for fmt in ("PDF", "XLSX"):
            options["format"] = fmt
            destination = self.root / ("ui-export." + fmt.lower())
            with patch("gui.report_builder.QFileDialog.getSaveFileName", return_value=(str(destination), "")):
                with patch("gui.report_builder.QMessageBox.information") as info:
                    with patch("gui.report_builder.QMessageBox.critical") as error:
                        run_report(None, options, {"engedelyek": items}, SITES, logo_path="/nonexistent/logo.png")
                        self.assertTrue(destination.is_file())
                        info.assert_called_once()
                        error.assert_not_called()
        workbook = load_workbook(self.root / "ui-export.xlsx")
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.active.max_row, 2)

    def test_run_report_failure_does_not_claim_success(self):
        options = self.dialog().get_options()
        with patch("gui.report_builder.QFileDialog.getSaveFileName", return_value=(str(self.root / "bad.pdf"), "")):
            with patch("gui.report_builder.export_report", side_effect=OSError("disk failure")):
                with patch("gui.report_builder.QMessageBox.information") as info:
                    with patch("gui.report_builder.QMessageBox.critical") as error:
                        with self.assertLogs("gui.report_builder", level="ERROR"):
                            run_report(None, options, {"engedelyek": [permit()]}, SITES)
                        info.assert_not_called()
                        error.assert_called_once()


if __name__ == "__main__":
    unittest.main()

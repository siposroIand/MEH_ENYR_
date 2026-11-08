from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook

from exporters.excel_exporter import export_to_excel
from exporters.report_builder_exporter import export_report_excel


class CategoryExportTests(unittest.TestCase):
    def test_custom_category_names_are_valid_and_never_overwrite_another_group(self):
        categories = ["Vízjogi / engedély", "vízjogi / engedély", "A" * 31 + " első", "A" * 31 + " második", "Összes"]
        items = [
            {"id": str(i), "kategoria": category, "hatarozatszam": f"ID/{i}"}
            for i, category in enumerate(categories)
        ]
        with tempfile.TemporaryDirectory() as temp:
            for kind in ("standard", "report"):
                with self.subTest(exporter=kind):
                    path = Path(temp) / f"{kind}.xlsx"
                    if kind == "standard":
                        export_to_excel(str(path), items, [], {"group_by": "kategoria"})
                    else:
                        export_report_excel(
                            str(path), {item["kategoria"]: [item] for item in items}, [],
                            ["kategoria", "hatarozatszam"],
                        )
                    book = load_workbook(path)
                    try:
                        sheets = [ws for ws in book if kind == "report" or ws.title != "Összes"]
                        self.assertEqual(len(sheets), len(categories))
                        self.assertEqual(len({name.casefold() for name in book.sheetnames}), len(book.sheetnames))
                        self.assertTrue(all(len(name) <= 31 for name in book.sheetnames))
                        exported = set()
                        for sheet in sheets:
                            headers = [cell.value for cell in sheet[1]]
                            column = headers.index("Határozat száma") + 1
                            exported.add(sheet.cell(2, column).value)
                        self.assertEqual(exported, {f"ID/{i}" for i in range(len(categories))})
                    finally:
                        book.close()

import base64
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication, QDialog

from core.document_ai import (
    AiCancelled, AiError, FIELDS, OpenAiClient, parse_result, prepare_document, validate_images,
)
from gui.document_ai_dialog import DocumentAiDialog
from gui.engedely_dialog import EngedelyDialog


def result_data():
    values = {
        "authority": ("Minta Kormányhivatal", "Kiadó: Minta Kormányhivatal"),
        "issued_on": ("2026-01-15", "Kelt: 2026. január 15."),
        "validity": ("2030-12-31", "Hatályos: 2030. december 31-ig"),
        "subject": ("Minta engedély", "Tárgy: Minta engedély"),
    }
    return {
        key: {"value": value, "status": "found", "page": 1, "evidence": evidence, "note": ""}
        for key, (value, evidence) in values.items()
    }


def image_payload():
    with Image.new("RGB", (100, 100), "white") as image, BytesIO() as buffer:
        image.save(buffer, "JPEG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")


class AiCoreTests(unittest.TestCase):
    def response(self, data=None):
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(data if data is not None else result_data())},
            ]}],
        }

    def test_request_uses_vision_strict_schema_no_storage_and_no_document_filename(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-real-key", "MEH_OPENAI_MODEL": "gpt-4.1"}):
            with patch("core.document_ai.build_opener") as opener:
                opener.return_value.open.return_value = BytesIO(json.dumps(self.response()).encode())
                result = OpenAiClient().ai_extract([image_payload(), image_payload()], True)
        request = opener.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(len(payload["input"][0]["content"]), 4)
        self.assertEqual(payload["input"][0]["content"][1]["type"], "input_image")
        self.assertIn("nem megbízható adatforrás", payload["instructions"])
        self.assertNotIn("tools", payload)
        self.assertEqual(result.authority.value, "Minta Kormányhivatal")

    def test_no_consent_no_key_or_invalid_images_never_contact_provider(self):
        with patch("core.document_ai.build_opener") as opener:
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                self.assertFalse(OpenAiClient().ai_capabilities()["enabled"])
                with self.assertRaises(AiError):
                    OpenAiClient().ai_extract([image_payload()], True)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-real"}):
                for consent in (False, "true", 1):
                    with self.subTest(consent=consent), self.assertRaises(AiError):
                        OpenAiClient().ai_extract([image_payload()], consent)
                with self.assertRaises(AiError):
                    OpenAiClient().ai_extract(["https://example.invalid/document"], True)
                with self.assertRaises(AiError):
                    OpenAiClient().ai_extract([base64.b64encode(b"\xff\xd8\xffinvalid").decode()], True)
            opener.assert_not_called()

    def test_provider_failures_have_safe_explicit_messages_and_no_retry(self):
        for failure in (
            HTTPError("https://api.openai.com/v1/responses", 429, "private-body", {}, BytesIO(b"private-body")),
            TimeoutError("private-details"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-real"}):
                    with patch("core.document_ai.build_opener") as opener:
                        opener.return_value.open.side_effect = failure
                        with self.assertRaises(AiError) as caught:
                            OpenAiClient().ai_extract([image_payload()], True)
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_refused_incomplete_and_malformed_responses_are_not_applied(self):
        responses = [
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
            {"status": "completed", "output": []},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "not json"}]}]},
            self.response({"unexpected": "shape"}),
        ]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(AiError):
                OpenAiClient._parse_response(response, 1)

    def test_invalid_dates_evidence_pages_and_overlong_text_are_rejected(self):
        for key, update in (
            ("issued_on", {"value": "2026-02-30"}),
            ("issued_on", {"value": "indefinite"}),
            ("validity", {"value": "öt év"}),
            ("subject", {"value": "x" * 201}),
            ("authority", {"evidence": None}),
            ("authority", {"page": 2}),
            ("authority", {"page": True}),
        ):
            data = result_data()
            data[key].update(update)
            with self.subTest(key=key, update=update), self.assertRaises(AiError):
                parse_result(data, 1)

    def test_missing_uncertain_and_special_validities_remain_distinct(self):
        for validity in ("indefinite", "until_modified"):
            data = result_data()
            data["validity"]["value"] = validity
            self.assertEqual(parse_result(data, 1).validity.value, validity)
        data = result_data()
        data["validity"].update(value=None, status="uncertain", note="Csak a kezdődátum szerepel.")
        data["issued_on"].update(value=None, status="missing", page=None, evidence=None)
        parsed = parse_result(data, 1)
        self.assertEqual(parsed.validity.status, "uncertain")
        self.assertIsNone(parsed.issued_on.value)

    def test_expiry_before_issue_is_not_an_applicable_finding(self):
        data = result_data()
        data["validity"]["value"] = "2025-12-31"
        self.assertEqual(parse_result(data, 1).validity.status, "uncertain")

    def test_pdf_tiff_all_pages_and_limits_without_any_network(self):
        with tempfile.TemporaryDirectory() as temporary, Image.new("RGB", (400, 600), "white") as image:
            root = Path(temporary)
            for suffix in ("pdf", "tiff"):
                path = root / f"fixture.{suffix}"
                image.save(path, save_all=True, append_images=[image.copy()])
                with patch("core.document_ai.build_opener") as opener:
                    pages = prepare_document(path)
                    self.assertEqual(len(pages), 2)
                    validate_images(pages)
                    opener.assert_not_called()
                with patch("core.document_ai.MAX_PAGES", 1), self.assertRaisesRegex(AiError, "csonka"):
                    prepare_document(path)
            with self.assertRaises(AiCancelled):
                prepare_document(root / "missing.pdf", lambda: True)
            bad = root / "bad.pdf"
            bad.write_bytes(b"invalid")
            with self.assertRaises(AiError):
                prepare_document(bad)
            with patch("core.document_ai.MAX_TOTAL_BYTES", 1), self.assertRaises(AiError):
                validate_images([image_payload()])


class FakeAiClient:
    def __init__(self):
        self.calls = []
        self.response = parse_result(result_data(), 1)
        self.enabled = True
        self.delay = 0

    def ai_capabilities(self):
        return {"enabled": self.enabled, "model": "mock-model", "max_pages": 20}

    def ai_extract(self, images, consent):
        self.calls.append((images, consent))
        time.sleep(self.delay)
        return self.response


class AiDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "synthetic.png"
        with Image.new("RGB", (400, 600), "white") as image:
            image.save(self.path)
        self.client = FakeAiClient()
        self.dialog = DocumentAiDialog(self.path, self.client)
        self.addCleanup(self.dialog.deleteLater)
        self.wait()

    def wait(self):
        deadline = time.monotonic() + 10
        while self.dialog._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertIsNone(self.dialog._worker)

    def send(self):
        self.dialog.consent.setChecked(True)
        self.dialog.send_btn.click()
        self.wait()

    def test_browse_preview_requires_consent_and_separate_field_approval(self):
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(self.dialog.images), 1)
        self.assertFalse(self.dialog.send_btn.isEnabled())
        self.dialog.send_btn.click()
        self.assertEqual(self.client.calls, [])
        self.send()
        self.assertEqual(len(self.client.calls), 1)
        self.assertIs(self.client.calls[0][1], True)
        self.assertEqual(self.dialog.selected_fields(), {})
        self.assertFalse(self.dialog.apply_btn.isEnabled())
        self.dialog.checks["authority"].setChecked(True)
        self.assertEqual(self.dialog.selected_fields(), {"authority": "Minta Kormányhivatal"})
        self.dialog.apply_btn.click()
        self.assertEqual(self.dialog.result(), QDialog.Accepted)

    def test_uncertain_and_missing_fields_cannot_be_approved(self):
        data = result_data()
        data["validity"].update(value=None, status="uncertain")
        data["issued_on"].update(value=None, status="missing")
        self.client.response = parse_result(data, 1)
        self.send()
        self.assertFalse(self.dialog.checks["validity"].isEnabled())
        self.assertFalse(self.dialog.checks["issued_on"].isEnabled())

    def test_cancelling_in_flight_discards_result_and_keeps_thread_alive_until_finished(self):
        self.client.delay = .1
        self.dialog.consent.setChecked(True)
        self.dialog.send_btn.click()
        self.dialog.reject()
        self.assertIsNotNone(self.dialog._worker)
        self.wait()
        self.assertEqual(self.dialog.result(), QDialog.Rejected)
        self.assertEqual(self.dialog.selected_fields(), {})

    def test_absent_configuration_disables_send(self):
        self.dialog._received((self.dialog.images, {"enabled": False, "model": "mock"}))
        self.dialog._update_buttons()
        self.dialog.consent.setChecked(True)
        self.assertFalse(self.dialog.send_btn.isEnabled())
        self.assertIn("OPENAI_API_KEY", self.dialog.status.text())

    def test_approved_four_fields_map_to_existing_persistence_shape(self):
        permit = EngedelyDialog(
            telephelyek=[{"id": "site", "nev": "Site"}], attachments_base=str(self.root / "attachments"),
        )
        self.addCleanup(permit.deleteLater)
        original = permit._ai_snapshot()
        values = {key: item["value"] for key, item in result_data().items()}
        permit._apply_ai_fields(values, original)
        data = permit.get_data()
        self.assertEqual(data["kiado_hatosag"], values["authority"])
        self.assertEqual(data["letrejott"], values["issued_on"])
        self.assertEqual(data["lejarat"], values["validity"])
        self.assertEqual(data["engedely_nev"], values["subject"])
        self.assertEqual(permit.result(), QDialog.Rejected)
        permit._apply_ai_fields({"validity": "indefinite"}, permit._ai_snapshot())
        self.assertTrue(permit.rb_indef.isChecked())
        self.assertIsNone(permit.get_data()["lejarat"])

    def test_manually_changed_fields_are_not_overwritten(self):
        permit = EngedelyDialog(attachments_base=str(self.root / "attachments"))
        self.addCleanup(permit.deleteLater)
        original = permit._ai_snapshot()
        permit.kiado.setText("Kézzel megadott hatóság")
        permit.targy.setText("Kézzel megadott tárgy")
        permit.keltezes_cb.setChecked(True)
        permit.rb_until_mod.setChecked(True)
        permit._apply_ai_fields({key: item["value"] for key, item in result_data().items()}, original)
        self.assertEqual(permit.kiado.text(), "Kézzel megadott hatóság")
        self.assertEqual(permit.targy.text(), "Kézzel megadott tárgy")
        self.assertTrue(permit.rb_until_mod.isChecked())
        self.assertEqual(permit._ai_snapshot()["issued_on"], (True, original["issued_on"][1]))
        self.assertIn("nem írtuk felül", permit.ocr_status.text())

    def test_permit_ai_entry_point_applies_only_after_dialog_acceptance(self):
        directory = self.root / "attachments" / "permit"
        directory.mkdir(parents=True)
        (directory / "document.png").write_bytes(self.path.read_bytes())
        permit = EngedelyDialog(
            engedely={"id": "permit"}, attachments_base=str(directory.parent), ai_client=self.client,
        )
        self.addCleanup(permit.deleteLater)
        with patch("gui.engedely_dialog.DocumentAiDialog") as dialog_class:
            dialog = dialog_class.return_value
            dialog.exec.return_value = QDialog.Rejected
            dialog.selected_fields.return_value = {"authority": "Jóváhagyott hatóság"}
            permit._recognize_with_ai()
            self.assertEqual(permit.kiado.text(), "")
            dialog.selected_fields.assert_not_called()
            dialog.exec.return_value = QDialog.Accepted
            permit._recognize_with_ai()
            self.assertEqual(permit.kiado.text(), "Jóváhagyott hatóság")
            self.assertIs(dialog_class.call_args.args[1], self.client)

    def test_attachment_browsing_never_starts_external_ai(self):
        permit = EngedelyDialog(attachments_base=str(self.root / "attachments"), ai_client=self.client)
        self.addCleanup(permit.deleteLater)
        with patch("gui.engedely_dialog.QFileDialog.getOpenFileNames", return_value=([str(self.path)], "")):
            with patch("gui.engedely_dialog.OcrWorker"):
                with patch("gui.engedely_dialog.DocumentAiDialog") as dialog:
                    permit.add_attachment()
                    self.assertFalse(permit.attach_ai.isEnabled())
                    permit._ocr_finished()
                    dialog.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.assertTrue(permit.attach_ai.isEnabled())

"""Float's test suite.

Run with: python -m pytest tests/ -v
(or just `python tests/test_float.py` — it also runs standalone.)

Each test gets its own throwaway data directory so nothing here touches a
real installation. The HTTP tests spin up the real server on an ephemeral
port and talk to it exactly the way the browser does.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _fresh_data_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="float-test-"))
    os.environ["FLOAT_DATA_DIR"] = str(path)
    return path


# ---------------------------------------------------------------------------
# artifacts: the hand-rolled docx/pdf/xlsx writers
# ---------------------------------------------------------------------------

class ArtifactTests(unittest.TestCase):
    SAMPLE_MD = (
        "# Weekly Notice\n\n"
        "**KV Sector 8**\n\n"
        "This is a *sample* notice with `inline code` and a list:\n\n"
        "- First point\n- Second point\n\n"
        "1. Numbered one\n2. Numbered two\n\n"
        "> A quoted line.\n\n"
        "| Item | Qty |\n|------|-----|\n| Chalk | 10 |\n| Duster | 3 |\n\n"
        "---\n\nSigned, Anita Sharma\n"
    )

    def setUp(self):
        _fresh_data_dir()
        import importlib
        global artifacts
        import float.artifacts as artifacts
        importlib.reload(artifacts)

    def test_docx_is_a_valid_zip_with_expected_parts(self):
        data = artifacts.render("docx", self.SAMPLE_MD, "Weekly Notice", "Anita Sharma")
        self.assertGreater(len(data), 500)
        with tempfile.NamedTemporaryFile(suffix=".docx") as f:
            f.write(data)
            f.flush()
            zf = zipfile.ZipFile(f.name)
            self.assertIsNone(zf.testzip())
            names = zf.namelist()
            for expected in ("[Content_Types].xml", "word/document.xml", "word/styles.xml"):
                self.assertIn(expected, names)
            body = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("Weekly Notice", body)

    def test_xlsx_is_a_valid_zip_with_a_sheet(self):
        data = artifacts.render("xlsx", self.SAMPLE_MD, "Notice", "")
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as f:
            f.write(data)
            f.flush()
            zf = zipfile.ZipFile(f.name)
            self.assertIsNone(zf.testzip())
            self.assertIn("xl/worksheets/sheet1.xml", zf.namelist())

    def test_pdf_has_valid_header_and_trailer(self):
        data = artifacts.render("pdf", self.SAMPLE_MD, "Notice", "")
        self.assertTrue(data.startswith(b"%PDF-1."))
        self.assertIn(b"%%EOF", data[-32:])
        self.assertIn(b"/Type /Catalog", data)

    def test_csv_has_bom_for_excel_and_hindi_text(self):
        data = artifacts.render("csv", "| Name | Marks |\n|---|---|\n| \u0906\u0930\u0935 | 42 |\n", "Marks", "")
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("\u0906\u0930\u0935".encode("utf-8"), data)

    def test_html_is_self_contained_and_escapes_input(self):
        data = artifacts.render("html", "# <script>alert(1)</script>\n", "T", "")
        text = data.decode("utf-8")
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_unknown_kind_falls_back_to_plain_text(self):
        # render() is deliberately forgiving: an unrecognised kind never
        # crashes a chat turn, it just degrades to plain text.
        data = artifacts.render("docm", "# Heading\n\nSome *text*.", "T", "")
        text = data.decode("utf-8")
        self.assertIn("Heading", text)
        self.assertNotIn("#", text)


# ---------------------------------------------------------------------------
# security: hashing, at-rest encryption, rate limiting
# ---------------------------------------------------------------------------

class SecurityTests(unittest.TestCase):
    def setUp(self):
        _fresh_data_dir()
        import importlib
        global security
        import float.security as security
        importlib.reload(security)

    def test_password_hash_roundtrip(self):
        digest = security.hash_password("chalkboard sunrise")
        self.assertTrue(security.verify_password("chalkboard sunrise", digest))
        self.assertFalse(security.verify_password("wrong phrase entirely", digest))

    def test_password_problems_rejects_short_passwords(self):
        self.assertTrue(security.password_problems("short"))
        self.assertFalse(security.password_problems("a proper long passphrase"))

    def test_pin_problems_rejects_non_numeric(self):
        self.assertTrue(security.pin_problems("abcd"))
        self.assertTrue(security.pin_problems("123"))  # too short
        self.assertFalse(security.pin_problems("4321"))

    def test_secret_encrypt_roundtrip(self):
        blob = security.encrypt_secret("sk-super-secret-key")
        self.assertNotIn("sk-super-secret-key", blob)
        self.assertEqual(security.decrypt_secret(blob), "sk-super-secret-key")

    def test_mask_key_shows_only_last_four(self):
        masked = security.mask_key("sk-abcdefghijklmnop")
        self.assertTrue(masked.endswith("mnop"))
        self.assertNotIn("abcdef", masked)

    def test_rate_limiter_blocks_after_threshold(self):
        limiter = security.RateLimiter(limit=3, window=60)
        key = "test-actor"
        for _ in range(3):
            allowed, _ = limiter.check(key)
            self.assertTrue(allowed)
        allowed, wait = limiter.check(key)
        self.assertFalse(allowed)
        self.assertGreater(wait, 0)

    def test_boot_nonce_is_present_and_changes_per_process_seed(self):
        self.assertTrue(security.BOOT_NONCE)
        self.assertIsInstance(security.BOOT_NONCE, str)


# ---------------------------------------------------------------------------
# the HTTP server, exercised the way the browser does
# ---------------------------------------------------------------------------

class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = _fresh_data_dir()
        import importlib
        import float.config as config
        import float.db as db
        import float.server as server
        importlib.reload(config)
        importlib.reload(db)
        importlib.reload(server)
        cls.server_module = server
        cls.srv = server.serve("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def call(self, path, body=None, token=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            method=method or ("POST" if data else "GET"),
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_01_state_reports_setup_needed_on_a_fresh_install(self):
        status, data = self.call("/api/auth/state", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["setup_needed"])

    def test_02_first_signup_becomes_admin(self):
        status, data = self.call("/api/auth/signup", {
            "email": "anita@school.edu.in", "name": "Anita Sharma",
            "password": "chalkboard sunrise", "school": "KV Sector 8",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["user"]["role"], "admin")
        self.__class__.token = data["token"]

    def test_03_bootstrap_has_every_field_the_front_end_reads(self):
        status, data = self.call("/api/bootstrap", token=self.token, method="GET")
        self.assertEqual(status, 200)
        for key in ("user", "greeting", "conversations", "toolkit", "toolkit_groups",
                    "classes", "tasks", "artifacts", "notices", "models", "keys",
                    "providers", "options", "capabilities", "usage", "own_work"):
            self.assertIn(key, data)
        self.assertEqual(len(data["toolkit"]), 19)
        self.assertIn("openrouter", {p["id"] for p in data["providers"]})

    def test_04_class_roster_parses_from_free_text(self):
        status, data = self.call("/api/classes", {
            "name": "9A", "grade": "9", "section": "A", "subject": "Maths",
            "roster": "1 Aarav Kumar\n2 Diya Sharma\n3 Ishaan Patel",
        }, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(data["students"], 3)

    def test_05_toolkit_entry_builds_a_prompt(self):
        status, data = self.call("/api/toolkit/worksheet", {
            "values": {"subject": "Science", "klass": "9A", "topic": "Photosynthesis"},
        }, token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("prompt", data)
        self.assertGreater(len(data["prompt"]), 20)

    def test_06_pin_can_be_set_and_used_to_sign_in(self):
        status, data = self.call("/api/me/pin", {
            "password": "chalkboard sunrise", "pin": "4321",
        }, token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(data["has_pin"])

        status, data = self.call("/api/auth/signin", {
            "email": "anita@school.edu.in", "pin": "4321",
        })
        self.assertEqual(status, 200)
        self.assertIn("token", data)

    def test_07_wrong_pin_is_rejected(self):
        status, data = self.call("/api/auth/signin", {
            "email": "anita@school.edu.in", "pin": "0000",
        })
        self.assertEqual(status, 401)

    def test_08_admin_overview_reflects_the_one_teacher(self):
        status, data = self.call("/api/admin/overview", token=self.token, method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(data["totals"]["teachers"], 1)

    def test_09_non_admin_cannot_reach_admin_routes(self):
        # self_signup is off by default, so the admin adds the second
        # teacher from the admin panel rather than the teacher signing up.
        status, data = self.call("/api/admin/users", {
            "email": "raj@school.edu.in", "name": "Raj Verma",
        }, token=self.token)
        self.assertEqual(status, 200)
        temp_password = data["temp_password"]

        status, data = self.call("/api/auth/signin", {
            "email": "raj@school.edu.in", "password": temp_password,
        })
        self.assertEqual(status, 200)
        teacher_token = data["token"]

        status, _ = self.call("/api/admin/overview", token=teacher_token, method="GET")
        self.assertEqual(status, 403)

    def test_10_missing_token_is_unauthorized(self):
        status, _ = self.call("/api/bootstrap", method="GET")
        self.assertEqual(status, 401)

    def test_11_org_settings_round_trip(self):
        status, data = self.call("/api/admin/org", {
            "school_name": "KV Sector 8", "self_signup": "1",
            "allowed_providers": ["anthropic"],
        }, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data["org"]["allowed_providers"]), ["anthropic"])

    def test_12_notice_appears_for_other_teachers(self):
        status, data = self.call("/api/admin/notice", {
            "title": "PTM Friday", "body": "Bring report cards.",
        }, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["notices"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

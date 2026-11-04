import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import auth


class AuthConfigTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parent / "artifacts"
        artifacts.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.password = secrets.token_urlsafe(24)
        for name, value in {
            "DB_PATH": self.root / "membership.db",
            "JWT_SECRET": secrets.token_urlsafe(48),
            "JWT_EXPIRES_MINUTES": 60,
            "ADMIN_USERNAME": "bootstrap",
            "ADMIN_EMAIL": "bootstrap@example.test",
            "ADMIN_PASSWORD": self.password,
        }.items():
            current = patch.object(auth, name, value)
            current.start()
            self.addCleanup(current.stop)

    def test_invalid_jwt_configuration_rejected_before_database_creation(self):
        for value in ("", "too-short", "change-me-" + "x" * 40):
            with self.subTest(value=value), patch.object(auth, "JWT_SECRET", value):
                with self.assertRaisesRegex(RuntimeError, "JWT_SECRET"):
                    auth.init_db()
                with self.assertRaisesRegex(RuntimeError, "JWT_SECRET"):
                    auth.create_access_token("user")
                with self.assertRaisesRegex(RuntimeError, "JWT_SECRET"):
                    auth.decode_access_token("invalid")
                self.assertFalse(auth.DB_PATH.exists())

    def test_nonpositive_token_lifetime_rejected(self):
        with patch.object(auth, "JWT_EXPIRES_MINUTES", 0):
            with self.assertRaisesRegex(RuntimeError, "JWT_EXPIRES_MINUTES"):
                auth.init_db()

    def test_invalid_bootstrap_password_cannot_create_admin(self):
        for password in ("", "too-short", "a" * 73, "中" * 25):
            with self.subTest(byte_length=len(password.encode())), patch.object(auth, "ADMIN_PASSWORD", password):
                with self.assertRaisesRegex(RuntimeError, "ADMIN_PASSWORD"):
                    auth.init_db()
                with sqlite3.connect(auth.DB_PATH) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_invalid_bootstrap_email_rejected(self):
        with patch.object(auth, "ADMIN_EMAIL", "invalid-email"):
            with self.assertRaisesRegex(RuntimeError, "ADMIN_EMAIL"):
                auth.init_db()

    def test_initial_admin_login_and_jwt_round_trip(self):
        auth.init_db()
        user = auth.authenticate("bootstrap", self.password)
        self.assertEqual(user["role"], "admin")
        token = auth.create_access_token(user["id"])
        self.assertEqual(auth.decode_access_token(token)["sub"], user["id"])
        with sqlite3.connect(auth.DB_PATH) as conn:
            stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotEqual(stored, self.password)
        self.assertTrue(auth.verify_password(self.password, stored))

    def test_restart_preserves_existing_admin_and_regular_users(self):
        auth.init_db()
        admin = auth.authenticate("bootstrap", self.password)
        user = auth.register_user("reader@example.test", "reader-password-123")
        for password in ("", secrets.token_urlsafe(24)):
            with patch.object(auth, "ADMIN_PASSWORD", password):
                auth.init_db()
        self.assertEqual(auth.authenticate("bootstrap", self.password)["id"], admin["id"])
        self.assertEqual(auth.get_user_by_id(user["id"])["role"], "user")

    def test_existing_regular_username_is_not_promoted_to_admin(self):
        auth.init_db()
        user = auth.register_user("reader@example.test", "reader-password-123")
        with patch.object(auth, "ADMIN_USERNAME", user["username"]):
            with self.assertRaisesRegex(RuntimeError, "ADMIN_USERNAME"):
                auth.init_db()
        self.assertEqual(auth.get_user_by_id(user["id"])["role"], "user")

    def _isolated_app(self):
        source = Path(__file__).resolve().parent.parent
        shutil.copytree(source / "app", self.root / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.root / "static").mkdir()
        (self.root / "static" / "index.html").write_text("test", encoding="utf-8")
        (self.root / ".env").write_text(
            f"JWT_SECRET={secrets.token_urlsafe(48)}\n"
            f"ADMIN_PASSWORD={self.password}\n"
            "ADMIN_USERNAME=bootstrap\nADMIN_EMAIL=bootstrap@example.test\n"
            "JWT_EXPIRES_MINUTES=37\nMEMBERSHIP_PLAN_DAYS=17\n"
            "MEMBERSHIP_PRICE_CENTS=1234\nMEMBERSHIP_PLAN_NAME=test-plan\n"
            "ASR_ENABLED=false\nVIDEO_ANALYSIS_ENABLED=false\n",
            encoding="utf-8",
        )
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "PYTHON_DOTENV_DISABLED"}
               and not key.startswith(("ADMIN_", "JWT_", "MEMBERSHIP_", "DEEPSEEK_", "QWEN_", "BILIBILI_"))}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _run_child(self, script, env):
        result = subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=self.root,
            env=env, text=True, capture_output=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_fresh_app_loads_dotenv_before_auth_and_membership(self):
        env = self._isolated_app()
        result = self._run_child(
            "from app import main, auth, membership\n"
            "import json, os\n"
            "user = auth.authenticate('bootstrap', os.environ['ADMIN_PASSWORD'])\n"
            "token = auth.create_access_token(user['id'])\n"
            "assert auth.decode_access_token(token)['sub'] == user['id']\n"
            "print(json.dumps({'role': user['role'], 'minutes': auth.JWT_EXPIRES_MINUTES, "
            "'plan_days': membership.PLAN_DAYS, 'price': membership.PRICE_CENTS, "
            "'health': main.health()['status']}))\n",
            env,
        )
        self.assertEqual(result, {"role": "admin", "minutes": 37, "plan_days": 17, "price": 1234, "health": "ok"})

    def test_external_environment_overrides_dotenv_for_direct_module_imports(self):
        env = self._isolated_app()
        env["JWT_EXPIRES_MINUTES"] = "19"
        env["MEMBERSHIP_PLAN_DAYS"] = "23"
        result = self._run_child(
            "from app import membership, auth\nimport json\n"
            "print(json.dumps({'minutes': auth.JWT_EXPIRES_MINUTES, 'plan_days': membership.PLAN_DAYS}))\n",
            env,
        )
        self.assertEqual(result, {"minutes": 19, "plan_days": 23})


if __name__ == "__main__":
    unittest.main()

"""Offline regression tests; all OAuth and Gmail network boundaries are mocked."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_gmail_connection as connection


class ConnectionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = Path(self.directory.name) / "client.json"
        self.client.write_text("{}", encoding="utf-8")
        self.token = Path(self.directory.name) / "token.json"
        self.email = "tester@example.com"
        self.creds = Mock(valid=True, expired=False, scopes=[connection.SCOPE], granted_scopes=None)
        self.creds.to_json.return_value = json.dumps(
            {"token": "fake", "scopes": [connection.SCOPE]}
        )
        self.loader = self.enterContext(
            patch.object(
                connection.Credentials, "from_authorized_user_info", return_value=self.creds
            )
        )
        self.flow = self.enterContext(
            patch.object(connection.InstalledAppFlow, "from_client_secrets_file")
        )
        self.flow.return_value.run_local_server.return_value = self.creds
        self.build = self.enterContext(patch.object(connection, "build"))
        self.build.return_value.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": self.email
        }

    def cache(self, **updates):
        data = {"token": "fake", "scopes": [connection.SCOPE], "verified_email": self.email}
        data.update(updates)
        self.token.write_text(json.dumps(data), encoding="utf-8")
        return self.token.read_bytes()

    def run_check(self, answer=connection.CONFIRMATION):
        argv = [
            "test",
            "--expected-email",
            self.email,
            "--client-secrets",
            str(self.client),
            "--token-file",
            str(self.token),
        ]
        output = io.StringIO()

        def confirm(_):
            self.assertIn(self.email, output.getvalue())
            return answer

        with (
            patch("sys.argv", argv),
            patch("builtins.input", side_effect=confirm),
            contextlib.redirect_stdout(output),
        ):
            return connection.main()

    def test_cancel_never_authorizes_or_accesses_gmail(self):
        self.assertEqual(self.run_check("cancel"), 1)
        self.loader.assert_not_called()
        self.flow.assert_not_called()
        self.build.assert_not_called()
        self.assertFalse(self.token.exists())

    def test_incompatible_cache_rejected_before_use_or_refresh(self):
        for updates in (
            {"verified_email": None},
            {"verified_email": "other@example.com"},
            {"scopes": None},
            {"scopes": []},
            {"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]},
            {"scopes": [connection.SCOPE, "extra"]},
        ):
            with self.subTest(updates=updates):
                original = self.cache(**updates)
                self.assertEqual(self.run_check(), 3)
                self.assertEqual(self.token.read_bytes(), original)
        self.loader.assert_not_called()
        self.creds.refresh.assert_not_called()
        self.flow.assert_not_called()
        self.build.assert_not_called()

    def test_valid_cache_keeps_original_scopes(self):
        original = json.loads(self.cache())
        self.assertEqual(self.run_check(), 0)
        self.loader.assert_called_once_with(original)
        self.flow.assert_not_called()
        self.build.return_value.users.return_value.getProfile.assert_called_once_with(userId="me")

    def test_new_authorization_saves_verified_mailbox(self):
        self.assertEqual(self.run_check(), 0)
        self.assertEqual(json.loads(self.token.read_text())["verified_email"], self.email)
        self.flow.assert_called_once_with(str(self.client), scopes=[connection.SCOPE])

    def test_wrong_or_missing_mailbox_does_not_save(self):
        for address in ("wrong@example.com", None):
            with self.subTest(address=address):
                self.build.return_value.users.return_value.getProfile.return_value.execute.return_value = {
                    "emailAddress": address
                }
                self.assertEqual(self.run_check(), 3)
                self.assertFalse(self.token.exists())

    def test_failed_profile_does_not_overwrite_cache(self):
        original = self.cache()
        self.build.return_value.users.return_value.getProfile.return_value.execute.side_effect = (
            ValueError("fake failure")
        )
        self.assertEqual(self.run_check(), 4)
        self.assertEqual(self.token.read_bytes(), original)

    def test_refresh_preserves_binding(self):
        self.cache()
        self.creds.expired = True
        self.assertEqual(self.run_check(), 0)
        self.creds.refresh.assert_called_once()
        self.assertEqual(json.loads(self.token.read_text())["verified_email"], self.email)

    def test_broader_granted_scopes_are_not_used_or_saved(self):
        self.creds.granted_scopes = [connection.SCOPE, "extra"]
        self.assertEqual(self.run_check(), 3)
        self.build.assert_not_called()
        self.assertFalse(self.token.exists())


if __name__ == "__main__":
    unittest.main()

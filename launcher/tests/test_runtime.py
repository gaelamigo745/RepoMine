import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import repomine_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def test_profile_preserves_others_and_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launcher_profiles.json"
            original = {"profiles": {"personal": {"name": "Personal"}}, "settings": {"keep": True}}
            path.write_text(json.dumps(original), encoding="utf-8")
            runtime.configure_profile(folder, {}, "neoforge-21.1.233", "java.exe")
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["profiles"]["personal"], original["profiles"]["personal"])
            self.assertEqual(result["settings"], original["settings"])
            self.assertEqual(result["profiles"]["repomine"]["lastVersionId"], "neoforge-21.1.233")
            backup = next(Path(folder).glob("*.bak"))
            self.assertEqual(json.loads(backup.read_text()), original)
            runtime.configure_profile(folder, {}, "neoforge-21.1.233", "java.exe")
            self.assertEqual(len(list(Path(folder).glob("*.bak"))), 1)

    def test_broken_profile_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launcher_profiles.json"
            path.write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                runtime.configure_profile(folder, {}, "neoforge-21.1.233", "java.exe")
            self.assertEqual(path.read_text(), "broken")

    def test_server_preserves_and_deduplicates(self):
        import nbtlib
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "servers.dat"
            nbtlib.File({"servers": nbtlib.List[nbtlib.Compound]([
                nbtlib.Compound({"name": nbtlib.String("Other"), "ip": nbtlib.String("other.example")})
            ])}).save(path, gzipped=False)
            self.assertTrue(runtime.configure_server(folder, {}))
            self.assertFalse(runtime.configure_server(folder, {}))
            entries = nbtlib.load(path, gzipped=False)["servers"]
            self.assertEqual(len(entries), 2)
            self.assertEqual(str(entries[0]["ip"]), "other.example")
            self.assertEqual(str(entries[1]["ip"]), "tecserver.playit.plus")
            self.assertEqual(len(list(Path(folder).glob("*.bak"))), 1)

    def test_missing_session_never_starts_process(self):
        with patch("repomine_runtime.subprocess.Popen") as process:
            with self.assertRaises(runtime.DirectLaunchUnavailable):
                runtime.launch_game(".", {}, {})
            process.assert_not_called()

    def test_prepare_pins_loader_and_java(self):
        from minecraft_launcher_lib import install, mod_loader
        with tempfile.TemporaryDirectory() as folder, patch.object(install, "install_minecraft_version") as vanilla, patch.object(
                mod_loader, "get_mod_loader") as loader, patch.object(runtime, "_java", return_value="java21.exe"), patch.object(
                runtime, "configure_profile", return_value="RepoMine") as profile, patch.object(runtime, "configure_server") as server:
            loader.return_value.install.return_value = "neoforge-21.1.233"
            result = runtime.prepare_game(folder, {})
            self.assertEqual(vanilla.call_args.args[0], "1.21.1")
            self.assertEqual(loader.return_value.install.call_args.kwargs["loader_version"], "21.1.233")
            self.assertEqual(loader.return_value.install.call_args.kwargs["java"], "java21.exe")
            self.assertEqual(result["version_id"], "neoforge-21.1.233")
            profile.assert_called_once()
            server.assert_called_once()

    def test_launch_uses_authenticated_session_without_logging_token(self):
        from minecraft_launcher_lib import command
        with tempfile.TemporaryDirectory() as folder:
            version_path = Path(folder) / "versions" / "neoforge-21.1.233"
            version_path.mkdir(parents=True)
            (version_path / "neoforge-21.1.233.json").write_text("{}")
            messages = []
            with patch.object(runtime, "_java", return_value="java21.exe"), patch.object(
                    command, "get_minecraft_command", return_value=["java21.exe", "token-secret"]) as cmd, patch.object(runtime.subprocess, "Popen") as process:
                runtime.launch_game(folder, {}, {"session": {"id": "uuid", "name": "Player", "access_token": "token-secret"}}, messages.append)
                options = cmd.call_args.args[2]
                self.assertEqual(options["token"], "token-secret")
                self.assertEqual(options["executablePath"], "java21.exe")
                process.assert_called_once()
                self.assertNotIn("token-secret", " ".join(messages))

    def test_login_checks_state_and_discards_refresh_token(self):
        from minecraft_launcher_lib import microsoft_account
        with patch.object(microsoft_account, "parse_auth_code_url", return_value="code") as parse, patch.object(
                microsoft_account, "complete_login", return_value={"id": "uuid", "name": "Player", "access_token": "token", "refresh_token": "secret"}) as login:
            session = runtime.complete_login("app", "http://localhost", "http://localhost?code=code&state=state", "state", "verifier")
            parse.assert_called_once_with("http://localhost?code=code&state=state", "state")
            login.assert_called_once_with("app", None, "http://localhost", "code", "verifier")
            self.assertNotIn("refresh_token", session)
            with self.assertRaises(ValueError):
                runtime.complete_login("app", "http://localhost", "https://wrong.example/?code=x", "state", "verifier")

    def test_login_failure_does_not_expose_credentials(self):
        import traceback
        from minecraft_launcher_lib import microsoft_account
        response_url = "http://localhost?code=secret-token"
        with patch.object(microsoft_account, "parse_auth_code_url", side_effect=ValueError("secret-token")):
            try:
                runtime.complete_login("app", "http://localhost", response_url, "state", "verifier")
            except RuntimeError:
                self.assertNotIn("secret-token", traceback.format_exc())
            else:
                self.fail("Login should fail")

    def test_network_defaults_are_bounded_and_restored(self):
        import requests
        with patch.object(requests.sessions.Session, "request", return_value=None) as original:
            with runtime._bounded_network():
                requests.Session().get("https://example.com")
            self.assertIs(requests.sessions.Session.request, original)
            self.assertEqual(original.call_args.kwargs["timeout"], (15, 90))
            self.assertTrue(original.call_args.kwargs["verify"])

    def test_installer_command_uses_java_and_hidden_window(self):
        from minecraft_launcher_lib.mod_loader import _neoforge
        with tempfile.TemporaryDirectory() as folder, patch.object(_neoforge, "download_file"), patch.object(
                _neoforge, "do_vanilla_launcher_profiles_exists", return_value=True), patch.object(_neoforge.subprocess, "run") as run:
            _neoforge.Neoforge().install("1.21.1", folder, {}, "java21.exe", "21.1.233")
            args = run.call_args.args[0]
            self.assertEqual(args[0:2], ["java21.exe", "-jar"])
            self.assertEqual(args[-2:], ["--install-client", folder])
            self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()

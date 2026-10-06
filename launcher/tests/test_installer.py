import io
import unittest
from unittest.mock import patch, Mock

import instalador_mods_github as ui


class InstallerTests(unittest.TestCase):
    def test_version_comparison_and_invalid_release(self):
        self.assertGreater(ui.version_tuple("v1.10.0"), ui.version_tuple("1.9.0"))
        with self.assertRaises(ValueError):
            ui.version_tuple("1.1.0-beta")

    def test_update_prefers_api_digest(self):
        digest = "a" * 64
        with patch.object(ui.urllib.request, "urlopen") as network:
            self.assertEqual(ui.update_digest({}, {"digest": "sha256:" + digest}), digest)
        network.assert_not_called()

    def test_update_checksum_fallback_and_duplicate_rejection(self):
        digest = "a" * 64
        release = {"assets": [{"name": "SHA256SUMS.txt", "browser_download_url":
                   "https://github.com/Qmigo745/RepoMine/releases/download/v1.1.0/SHA256SUMS.txt"}]}
        line = (digest + "  InstaladorModsMinecraft.exe\r\n").encode()
        with patch.object(ui.urllib.request, "urlopen", return_value=io.BytesIO(line)):
            self.assertEqual(ui.update_digest(release, {}), digest)
        with patch.object(ui.urllib.request, "urlopen", return_value=io.BytesIO(line * 2)):
            with self.assertRaises(ValueError):
                ui.update_digest(release, {})

    def test_source_mode_update_never_replaces_python(self):
        app = object.__new__(ui.ModInstallerApp)
        app.set_status = Mock()
        with patch.object(ui.sys, "frozen", False, create=True), patch.object(ui, "download_json") as fetch:
            app.check_for_updates(True)
        fetch.assert_not_called()

    def test_background_error_is_delivered_without_tk_calls(self):
        app = object.__new__(ui.ModInstallerApp)
        app.is_busy = False
        app.minecraft_dir = Mock(get=Mock(return_value="test-game"))
        app.save_settings = Mock()
        app.action_buttons = []
        app.path_entry = Mock()
        app.folder_button = Mock()
        app.progress = Mock()
        import queue
        app.events = queue.Queue()
        def action():
            raise RuntimeError("Prueba de error")
        def immediate_thread(target, **kwargs):
            return Mock(start=target)
        with patch.object(ui.threading, "Thread", side_effect=immediate_thread):
            app.run_threaded(action)
        events = []
        while not app.events.empty():
            events.append(app.events.get())
        self.assertIn(("error", "Prueba de error"), events)
        self.assertEqual(events[-1], ("done", None))


if __name__ == "__main__":
    unittest.main()

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import repomine_core as core


def entry(name, payload=b"new", **extra):
    return {"file": name, "url": "https://example.test/" + name,
            "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload), **extra}


def manifest(*mods, configs=None):
    return {"schema_version": 2, "pack_name": "Test", "minecraft_version": "1.21.1",
            "loader": "NeoForge", "loader_version": "21.1.233",
            "mods": list(mods) or [entry("main.jar")], "configs": configs or []}


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}


class CoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def put(self, relative, content):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def state(self, files):
        core.atomic_json(self.root / ".repomine/state.json", {
            "schema_version": 1, "pack_name": "Test",
            "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}})

    def synchronize(self, data):
        with patch.object(core.urllib.request, "urlopen", side_effect=lambda *a, **k: Response(b"new")):
            return core.synchronize(self.root, data, log=lambda _: None)

    def test_validation_rejects_traversal_windows_aliases_and_case_duplicates(self):
        for name in ("../escape.jar", "sub/a.jar", "C:bad.jar", "CON.jar", "bad.jar.", "a\\b.jar"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                core.validate_manifest(manifest(entry(name)))
        with self.assertRaises(ValueError):
            core.validate_manifest(manifest(entry("A.jar"), entry("a.jar")))
        data = manifest()
        data["schema_version"] = True
        with self.assertRaises(ValueError):
            core.validate_manifest(data)

    def test_corrupt_download_keeps_previous_and_cleans_temporary(self):
        target = self.put("mods/main.jar", b"old")
        with patch.object(core.urllib.request, "urlopen", side_effect=lambda *a, **k: Response(b"bad")), \
                patch.object(core.time, "sleep"), self.assertRaises(ValueError):
            core.download_verified("https://example.test/mod", target, entry("main.jar")["sha256"])
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(target.parent.glob("*.download")), [])

    def test_failed_group_download_applies_nothing(self):
        target = self.put("mods/main.jar", b"old")
        def response(request, **kwargs):
            if request.full_url.endswith("second.jar"):
                raise OSError("network unavailable")
            return Response(b"new")
        with patch.object(core.urllib.request, "urlopen", side_effect=response), \
                patch.object(core.time, "sleep"), self.assertRaises(RuntimeError):
            core.synchronize(self.root, manifest(entry("main.jar"), entry("second.jar")), log=lambda _: None)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse((self.root / "mods/second.jar").exists())
        self.assertFalse((self.root / ".repomine/state.json").exists())

    def test_retired_owned_backed_up_personal_preserved(self):
        self.put("mods/retired.jar", b"owned")
        self.put("mods/personal.jar", b"personal")
        self.state({"mods/retired.jar": b"owned"})
        self.synchronize(manifest())
        self.assertFalse((self.root / "mods/retired.jar").exists())
        self.assertEqual((self.root / "mods/personal.jar").read_bytes(), b"personal")
        backups = list((self.root / ".repomine/backups").glob("*/mods/retired.jar"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"owned")
        state = json.loads((self.root / ".repomine/state.json").read_text())
        self.assertEqual(state["schema_version"], 1)
        self.assertNotIn("mods/personal.jar", state["files"])

    def test_customized_retired_owned_is_preserved(self):
        self.put("mods/retired.jar", b"user modified")
        self.state({"mods/retired.jar": b"original"})
        self.synchronize(manifest())
        self.assertEqual((self.root / "mods/retired.jar").read_bytes(), b"user modified")

    def test_default_config_customization_survives_update_and_retirement(self):
        self.put("config/preferences.toml", b"custom")
        self.state({"config/preferences.toml": b"original"})
        data = manifest(configs=[entry("preferences.toml", policy="default")])
        self.synchronize(data)
        self.assertEqual((self.root / "config/preferences.toml").read_bytes(), b"custom")
        self.synchronize(manifest())
        self.assertEqual((self.root / "config/preferences.toml").read_bytes(), b"custom")

    def test_default_preexisting_config_is_not_adopted(self):
        self.put("config/preferences.toml", b"custom")
        self.synchronize(manifest(configs=[entry("preferences.toml")]))
        state = json.loads((self.root / ".repomine/state.json").read_text())
        self.assertNotIn("config/preferences.toml", state["files"])

    def test_inspect_does_not_create_directories(self):
        destination = self.root / "absent"
        report = core.inspect_pack(destination, manifest())
        self.assertEqual(report["missing"], ["mods/main.jar"])
        self.assertFalse(destination.exists())

    def test_failed_state_write_rolls_back_replacements_additions_and_retirements(self):
        self.put("mods/main.jar", b"old")
        self.put("mods/retired.jar", b"retired")
        self.state({"mods/main.jar": b"old", "mods/retired.jar": b"retired"})
        state_before = (self.root / ".repomine/state.json").read_bytes()
        with patch.object(core, "atomic_json", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.synchronize(manifest(entry("main.jar"), entry("added.jar")))
        self.assertEqual((self.root / "mods/main.jar").read_bytes(), b"old")
        self.assertEqual((self.root / "mods/retired.jar").read_bytes(), b"retired")
        self.assertFalse((self.root / "mods/added.jar").exists())
        self.assertEqual((self.root / ".repomine/state.json").read_bytes(), state_before)

    def test_unknown_state_schema_is_rejected_before_mod_changes(self):
        self.put(".repomine/state.json", b'{"schema_version":99,"files":{}}')
        with self.assertRaises(ValueError):
            self.synchronize(manifest())
        self.assertFalse((self.root / "mods").exists())

    def test_lock_prevents_second_install_and_releases(self):
        with core.PackLock(self.root):
            with self.assertRaises(RuntimeError):
                with core.PackLock(self.root):
                    self.fail("Second lock unexpectedly acquired")
        with core.PackLock(self.root):
            pass


if __name__ == "__main__":
    unittest.main()

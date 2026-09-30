import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import generar_manifest as publisher


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("mods", "Resourcepacks", "shared-config"):
            (self.root / name).mkdir()
        for key, path in {"PROJECT_DIR": self.root, "MODS_PATH": self.root / "mods",
                "RESOURCEPACKS_PATH": self.root / "Resourcepacks",
                "SHARED_CONFIG_PATH": self.root / "shared-config",
                "MANIFEST_PATH": self.root / "manifest.json",
                "PACK_CONFIG_PATH": self.root / "pack_config.json"}.items():
            mock = patch.object(publisher, key, path)
            mock.start()
            self.addCleanup(mock.stop)

    def jar(self, filename, metadata):
        path = self.root / "mods" / filename
        with zipfile.ZipFile(path, "w") as jar:
            jar.writestr("META-INF/neoforge.mods.toml", metadata)
        return path

    def test_dependency_is_not_mod_and_inline_declaration(self):
        jar = self.jar("WallClimbing-v4.jar", 'mods = [{modId="wallclimbing"}]\n[[dependencies.wallclimbing]]\nmodId="fokusapi"')
        self.assertEqual(publisher.mod_id_from_jar(jar), "wallclimbing")

    def test_schema_and_default_config_policy(self):
        self.jar("main-v1.jar", '[[mods]]\nmodId="main"')
        (self.root / "shared-config" / "example.toml").write_text("enabled=true")
        self.assertTrue(publisher.generate_manifest(log=lambda _: None))
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["mods"][0]["ids"], ["main"])
        self.assertGreater(manifest["mods"][0]["size"], 0)
        self.assertEqual(manifest["configs"][0]["policy"], "default")
        self.assertEqual(manifest["server"]["address"], "tecserver.playit.plus")

    def test_duplicate_ids_leave_existing_manifest(self):
        self.jar("one.jar", '[[mods]]\nmodId="same"')
        self.jar("two.jar", '[[mods]]\nmodId="same"')
        target = self.root / "manifest.json"
        target.write_text("original")
        self.assertFalse(publisher.generate_manifest(log=lambda _: None))
        self.assertEqual(target.read_text(), "original")

    def test_multi_mod_uses_filename_match(self):
        jar = self.jar("main-v1.jar", '[[mods]]\nmodId="helper"\n[[mods]]\nmodId="main"')
        self.assertEqual(publisher.mod_id_from_jar(jar), "main")

    def test_snapshot_rejects_changed_or_added_files(self):
        jar = self.jar("main.jar", '[[mods]]\nmodId="main"')
        self.assertTrue(publisher.generate_manifest(log=lambda _: None))
        self.assertTrue(publisher.verify_manifest_assets(log=lambda _: None))
        jar.write_bytes(b"changed")
        self.assertFalse(publisher.verify_manifest_assets(log=lambda _: None))

    def test_publication_paths_exclude_transport_files(self):
        self.jar("main.jar", '[[mods]]\nmodId="main"')
        with patch.object(publisher, "run_git") as git:
            git.return_value.stdout = ""
            paths = publisher.publication_asset_paths()
        self.assertIn(":(glob)mods/*.jar", paths)
        self.assertNotIn("mods", paths)
        self.assertNotIn("mods.zip", paths)

    def test_nested_config_url_and_policy_override(self):
        self.jar("main.jar", '[[mods]]\nmodId="main"')
        directory = self.root / "shared-config" / "nested"
        directory.mkdir()
        (directory / "a b.toml").write_text("enabled=true")
        config = {"config_policies": {"nested/a b.toml": "managed"}}
        self.assertTrue(publisher.generate_manifest(log=lambda _: None, config=config))
        manifest = json.loads((self.root / "manifest.json").read_text())
        entry = manifest["configs"][0]
        self.assertTrue(entry["url"].endswith("shared-config/nested/a%20b.toml"))
        self.assertEqual(entry["policy"], "managed")


if __name__ == "__main__":
    unittest.main()

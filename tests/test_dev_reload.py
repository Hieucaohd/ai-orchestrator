import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dev_reload


class ReloadTests(unittest.TestCase):
    def test_source_edits_additions_and_deletions_are_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "server.py"
            source.write_text("VALUE = 1\n")
            original_stat = source.stat()
            first = dev_reload.snapshot(root)
            source.write_text("VALUE = 2\n")
            os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            second = dev_reload.snapshot(root)
            self.assertNotEqual(first, second)
            added = root / "new.py"
            added.write_text("VALUE = 3\n")
            self.assertNotEqual(second, dev_reload.snapshot(root))
            added.unlink()
            self.assertEqual(second, dev_reload.snapshot(root))

    def test_assets_are_watched_but_runtime_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("logs", "venv", "chrome-profile", "tests", "__pycache__"):
                (root / name).mkdir()
                (root / name / "ignored.py").write_text("ignored")
            (root / "talkshow.db").write_text("database")
            self.assertEqual(dev_reload.snapshot(root), {})
            for name in ("static", "templates"):
                (root / name).mkdir()
            (root / "static" / "app.js").write_text("const x = 1;")
            (root / "templates" / "index.html").write_text("<p>hello</p>")
            self.assertEqual(len(dev_reload.snapshot(root)), 2)

    def test_invalid_python_is_rejected_without_executing_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "server.py"
            source.write_text("raise RuntimeError('must not execute')\n")
            dev_reload.check_python([source])
            source.write_text("def broken(\n")
            with self.assertRaises(SyntaxError):
                dev_reload.check_python([source])

    def test_browser_reload_compares_the_version_embedded_in_the_page(self):
        with patch.object(dev_reload.sys, "argv", ["talkshow.py"]):
            page = dev_reload.inject_browser_reload("<body></body>")
        self.assertIn(json.dumps(dev_reload.VERSION), page)
        self.assertIn(dev_reload.VERSION_PATH, page)
        self.assertIn('cache: "no-store"', page)
        self.assertIn('new Event("beforedevreload")', page)

    def test_no_reload_and_browser_flags(self):
        with patch.object(dev_reload.sys, "argv", ["talkshow.py", "--no-reload", "--no-browser"]):
            self.assertEqual(dev_reload.inject_browser_reload("<body></body>"), "<body></body>")
            self.assertFalse(dev_reload.should_open_browser())
        with patch.object(dev_reload.sys, "argv", ["talkshow.py"]):
            with patch.dict(os.environ, {dev_reload.OPEN_BROWSER_ENV: "0"}):
                self.assertFalse(dev_reload.should_open_browser())
            with patch.dict(os.environ, {dev_reload.OPEN_BROWSER_ENV: "1"}):
                self.assertTrue(dev_reload.should_open_browser())


if __name__ == "__main__":
    unittest.main()

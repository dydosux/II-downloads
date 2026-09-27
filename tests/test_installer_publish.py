import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import publish_installer as publisher


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "release"
        self.assets.mkdir()
        part = self.assets / "payload.001"
        part.write_bytes(b"payload")
        manifest = {"parts": [{"name": part.name, "size": 7, "sha256": hashlib.sha256(b"payload").hexdigest()}]}
        (self.assets / "manifest.json").write_text(json.dumps(manifest))
        for name in ("II-Setup.exe", "SHA256SUMS.txt", "SD15-LICENSE.txt"):
            (self.assets / name).write_bytes(b"fixture")
        (self.root / "full-install-result.txt").write_text("OK")
        self.release = dict(id=1, tag_name="v2026.09.27", draft=True, html_url="https://example.test/release", assets=[])
        self.calls = []
        self.fail_upload = False

    def tearDown(self):
        self.temp.cleanup()

    def gh(self, *args):
        self.calls.append(args)
        if args[0] == "api" and args[1].endswith("releases?per_page=100"):
            return json.dumps([self.release])
        if args[:2] == ("release", "upload"):
            if self.fail_upload:
                raise RuntimeError("Upload interrupted")
            path = Path(args[3])
            self.release["assets"].append(dict(id=len(self.release["assets"])+10, name=path.name, state="uploaded", size=path.stat().st_size,
                                               digest="sha256:"+hashlib.sha256(path.read_bytes()).hexdigest()))
            return ""
        if args[:2] == ("release", "edit"):
            self.assertEqual(len(self.release["assets"]), 5)
            self.release["draft"] = False
            return ""
        raise AssertionError(args)

    def run_publish(self):
        with patch("sys.argv", ["publish", "--assets", str(self.assets)]), patch.object(publisher, "gh", side_effect=self.gh), patch.object(publisher.time, "sleep"):
            publisher.main()

    def test_publishes_only_after_all_assets_verified(self):
        self.run_publish()
        self.assertFalse(self.release["draft"])
        status = json.loads((self.root / "publish-status.json").read_text())
        self.assertEqual(status["state"], "published")
        self.assertEqual(sum(c[:2] == ("release", "edit") for c in self.calls), 1)

    def test_upload_failure_keeps_release_private(self):
        self.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.run_publish()
        self.assertTrue(self.release["draft"])
        self.assertFalse(any(c[:2] == ("release", "edit") for c in self.calls))
        self.assertEqual(json.loads((self.root / "publish-status.json").read_text())["state"], "error")

    def test_failed_local_install_prevents_upload(self):
        (self.root / "full-install-result.txt").write_text("Failed")
        with self.assertRaisesRegex(RuntimeError, "verification"):
            self.run_publish()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

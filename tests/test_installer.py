"""Run the compiled Windows installer engine against an actual local HTTP server."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import zipfile

from scripts.build_installer_payload import Parts, build


EXE = Path(__file__).resolve().parents[1] / "build/installer-test/II-Setup.exe"


@unittest.skipUnless(os.name == "nt" and EXE.exists(), "Compile build/installer-test/II-Setup.exe first")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "II.exe").write_bytes(b"test executable, never launched")
        (self.source / "weights.bin").write_bytes(os.urandom(70000))
        self.assets = self.root / "assets"
        self.manifest = build(self.source, self.assets, "https://example.invalid", part_size=16384)
        self.requests = []
        self.corrupt = False
        self.drop_once = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = owner.assets / self.path.lstrip("/")
                if not path.is_file():
                    self.send_error(404)
                    return
                body = path.read_bytes()
                start = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
                owner.requests.append((path.name, start))
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(body) - start))
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(body)-1}/{len(body)}")
                self.end_headers()
                if owner.corrupt:
                    body = bytes([body[0] ^ 1]) + body[1:]
                if owner.drop_once and start == 0:
                    owner.drop_once = False
                    self.wfile.write(body[:4000])
                    self.close_connection = True
                    return
                self.wfile.write(body[start:])

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.temp.cleanup()

    def config(self):
        path = self.root / "manifest.json"
        text = json.dumps(self.manifest)
        path.write_text(text, encoding="utf-8")
        cache = self.root / (".II-download-" + hashlib.sha256(text.encode()).hexdigest()[:16])
        cache.mkdir(exist_ok=True)
        return path, cache

    def install(self, target="installed"):
        manifest, _ = self.config()
        result = self.root / "result.txt"
        proc = subprocess.run([str(EXE), "--test-install", str(manifest), str(self.root / target),
                               f"http://127.0.0.1:{self.server.server_port}", str(result)], timeout=60)
        return proc.returncode, result.read_text(encoding="utf-8-sig")

    def test_resume_verify_extract_and_reuse_cache(self):
        _, cache = self.config()
        first = self.manifest["parts"][0]["name"]
        (cache / (first + ".partial")).write_bytes((self.assets / first).read_bytes()[:5000])
        # A complete but corrupt cached part must be replaced before extraction.
        second = self.manifest["parts"][1]["name"]
        (cache / second).write_bytes(b"X" * self.manifest["parts"][1]["size"])
        self.assertEqual(self.install(), (0, "OK"))
        self.assertIn((first, 5000), self.requests)
        for source in self.source.iterdir():
            self.assertEqual(source.read_bytes(), (self.root / "installed" / source.name).read_bytes())
        count = len(self.requests)
        self.assertEqual(self.install("second-install"), (0, "OK"))
        self.assertEqual(len(self.requests), count)

    def test_interrupted_response_is_resumed(self):
        self.drop_once = True
        self.assertEqual(self.install(), (0, "OK"))
        self.assertTrue(any(offset == 4000 for _, offset in self.requests))

    def test_bad_hash_never_installs(self):
        self.corrupt = True
        code, result = self.install()
        self.assertNotEqual(code, 0)
        self.assertIn("Контрольная сумма", result)
        self.assertFalse((self.root / "installed").exists())

    def test_existing_folder_is_preserved(self):
        target = self.root / "installed"
        target.mkdir()
        (target / "personal.txt").write_text("keep")
        self.assertNotEqual(self.install()[0], 0)
        self.assertEqual((target / "personal.txt").read_text(), "keep")
        self.assertEqual(self.requests, [])

    def test_zip_traversal_is_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("../escape.txt", b"bad")
            archive.writestr("II.exe", b"stub")
        data = stream.getvalue()
        name = "malicious.zip.001"
        (self.assets / name).write_bytes(data)
        self.manifest.update(installedBytes=7, fileCount=2,
                             parts=[dict(name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest())])
        code, result = self.install()
        self.assertNotEqual(code, 0)
        self.assertIn("Unsafe archive path", result)
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertFalse((self.root / "installed").exists())
        self.assertFalse(list(self.root.glob(".II-install-*")))


class PayloadTests(unittest.TestCase):
    def test_split_boundaries_preserve_exact_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            stream = Parts(Path(folder), 7)
            stream.write(b"abcdefghij")
            stream.write(b"klmnopqrstuv")
            stream.close()
            self.assertEqual(b"".join((Path(folder) / p["name"]).read_bytes() for p in stream.parts), b"abcdefghijklmnopqrstuv")
            self.assertTrue(all(p["size"] <= 7 for p in stream.parts))
            for p in stream.parts:
                self.assertEqual(p["sha256"], hashlib.sha256((Path(folder) / p["name"]).read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()

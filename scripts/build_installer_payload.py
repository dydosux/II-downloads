"""Build a ZIP split into immutable, SHA256-pinned GitHub release assets."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile


class Parts(io.RawIOBase):
    def __init__(self, output, limit):
        self.output, self.limit = output, limit
        self.position = 0
        self.current = None
        self.parts = []

    def writable(self):
        return True

    def seekable(self):
        return False

    def tell(self):
        return self.position

    def finish_part(self):
        if self.current is not None:
            self.current.close()
            self.parts.append(dict(name=self.name, size=self.count, sha256=self.digest.hexdigest()))
            self.current = None

    def write(self, data):
        length = len(data)
        view = memoryview(data)
        while view:
            if self.current is None:
                self.name = f"II-payload.zip.{len(self.parts) + 1:03d}"
                self.current = (self.output / self.name).open("xb")
                self.count = 0
                self.digest = hashlib.sha256()
            take = min(len(view), self.limit - self.count)
            self.current.write(view[:take])
            self.digest.update(view[:take])
            self.count += take
            self.position += take
            view = view[take:]
            if self.count == self.limit:
                self.finish_part()
        return length

    def close(self):
        self.finish_part()
        super().close()


def build(source, output, base_url, part_size=1536 * 1024**2):
    if not 0 < part_size < 2 * 1024**3:
        raise ValueError("Each release asset must be smaller than 2 GiB")
    source = source.resolve(strict=True)
    if not (source / "II.exe").is_file():
        raise ValueError("Source must contain II.exe")
    def distributable(path):
        rel = path.relative_to(source).as_posix()
        if any(p in {".git", "__pycache__"} for p in path.relative_to(source).parts):
            return False
        if path.suffix.lower() in {".pyc", ".pyo", ".log"}:
            return False
        if rel in {"data/photo/learned.json", "data/photo/memory.json", "data/photo/lessons.jsonl"}:
            return False
        if rel.startswith(("data/output/", "data/photos/", "data/inbox/", "data/logs/", "ComfyUI/input/", "ComfyUI/output/", "ComfyUI/temp/", "ComfyUI/user/")):
            return False
        if rel.startswith("data/learning/") and rel != "data/learning/materials/README.txt":
            return False
        if rel == "data/train.jsonl" and path.stat().st_size:
            raise ValueError("Training examples require review before public distribution")
        return True
    files = sorted(p for p in source.rglob("*") if p.is_file() and distributable(p))
    for path in files:
        rel = path.relative_to(source)
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != source.parent):
            raise ValueError(f"Symlink not allowed: {rel}")
        public_certificates = rel.as_posix() in {
            "runtime/Lib/site-packages/certifi/cacert.pem",
            "runtime/Lib/site-packages/pip/_vendor/certifi/cacert.pem",
        }
        if not public_certificates and (path.name.lower() == ".env" or path.suffix.lower() in {".sqlite", ".sqlite3", ".pem", ".key"}):
            raise ValueError(f"Review private file before distribution: {rel}")
    output.mkdir(parents=True, exist_ok=False)
    installed = sum(p.stat().st_size for p in files)
    stream = Parts(output, part_size)
    try:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for index, path in enumerate(files):
                archive.write(path, path.relative_to(source).as_posix())
                if index % 500 == 0:
                    print(f"Packed {index}/{len(files)} files", flush=True)
    finally:
        stream.close()
    manifest = dict(version="2026.09.27", baseUrl=base_url.rstrip("/"),
                    installedBytes=installed, fileCount=len(files), parts=stream.parts)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(dict(files=len(files), installed_bytes=installed, download_bytes=stream.tell(), parts=len(stream.parts))), flush=True)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    build(args.source, args.output, args.base_url)

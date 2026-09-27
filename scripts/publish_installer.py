"""Resume release uploads, verify GitHub asset hashes, then publish atomically.

Run after local installer validation. Uses the existing gh credential store.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import time

import psutil


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "GitHub CLI failed")
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--repo", default="dydosux/II-downloads")
    parser.add_argument("--tag", default="v2026.09.27")
    parser.add_argument("--wait-pids", nargs="*", type=int, default=[])
    args = parser.parse_args()
    root = args.assets.resolve(strict=True)
    status_file = root.parent / "publish-status.json"

    def status(state, message, **extra):
        data = dict(state=state, message=message, updated=time.strftime("%Y-%m-%d %H:%M:%S"), **extra)
        temp = status_file.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(status_file)
        print(data["updated"], message, flush=True)

    try:
        if (root.parent / "full-install-result.txt").read_text(encoding="utf-8-sig").strip() != "OK":
            raise RuntimeError("Full installation verification has not succeeded")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        names = [p["name"] for p in manifest["parts"]] + ["II-Setup.exe", "manifest.json", "SHA256SUMS.txt", "SD15-LICENSE.txt"]
        expected = {}
        for name in names:
            path = root / name
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            expected[name] = dict(size=path.stat().st_size, digest="sha256:" + digest)
        for part in manifest["parts"]:
            if expected[part["name"]] != dict(size=part["size"], digest="sha256:" + part["sha256"]):
                raise RuntimeError("Local payload changed: " + part["name"])

        processes = []
        for pid in args.wait_pids:
            try:
                process = psutil.Process(pid)
                command = process.cmdline()
                if process.name().lower() == "gh.exe" and "upload" in command and args.repo in command:
                    processes.append(process)
            except psutil.NoSuchProcess:
                pass
        status("uploading", "Waiting for uploads already in progress", parts=len(manifest["parts"]))
        while any(p.is_running() for p in processes):
            time.sleep(15)

        def release():
            # Draft tags are not resolved by /releases/tags until publication.
            releases = json.loads(gh("api", f"repos/{args.repo}/releases?per_page=100"))
            return next(r for r in releases if r["tag_name"] == args.tag)

        def matches(asset, wanted):
            return asset.get("state") == "uploaded" and asset.get("size") == wanted["size"] and asset.get("digest") == wanted["digest"]

        current = release()
        remote = {asset["name"]: asset for asset in current["assets"]}
        if not current["draft"]:
            if all(name in remote and matches(remote[name], wanted) for name, wanted in expected.items()):
                status("published", "Release was already published and verified", url=current["html_url"])
                return
            raise RuntimeError("Refusing to change an already published release")
        missing = []
        for name, wanted in expected.items():
            if name in remote and matches(remote[name], wanted):
                continue
            if name in remote:
                gh("api", "--method", "DELETE", f"repos/{args.repo}/releases/assets/{remote[name]['id']}")
            missing.append(name)
        status("uploading", f"Uploading {len(missing)} remaining assets", pending=missing)

        def upload(name):
            for attempt in range(4):
                try:
                    gh("release", "upload", args.tag, str(root / name), "--repo", args.repo)
                    print("Uploaded", name, flush=True)
                    return
                except RuntimeError:
                    assets = {a["name"]: a for a in release()["assets"]}
                    if name in assets:
                        if matches(assets[name], expected[name]):
                            return
                        gh("api", "--method", "DELETE", f"repos/{args.repo}/releases/assets/{assets[name]['id']}")
                    if attempt == 3:
                        raise
                    time.sleep(10 * (attempt+1))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(upload, missing))
        status("verifying", "Checking server-side SHA-256 and asset sizes")
        current = release()
        remote = {a["name"]: a for a in current["assets"]}
        for name, wanted in expected.items():
            if name not in remote or not matches(remote[name], wanted):
                raise RuntimeError("GitHub verification failed for " + name)
        gh("release", "edit", args.tag, "--repo", args.repo, "--draft=false", "--latest")
        current = release()
        if current["draft"]:
            raise RuntimeError("Release remained a draft")
        status("published", "Installer release is public and verified", url=current["html_url"],
               download=f"https://github.com/{args.repo}/releases/download/{args.tag}/II-Setup.exe")
    except Exception as exc:
        status("error", str(exc))
        raise


if __name__ == "__main__":
    main()

"""Exercise the Windows installer with its real payload, reusing local parts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    assets = root / 'release'
    manifest_path = assets / 'manifest.json'
    text = manifest_path.read_bytes().decode('utf-8')
    manifest = json.loads(text)
    cache = root / ('.II-download-' + hashlib.sha256(text.encode()).hexdigest()[:16])
    cache.mkdir(exist_ok=True)
    for part in manifest['parts']:
        source = assets / part['name']
        target = cache / part['name']
        if not target.exists():
            os.link(source, target)
        if not os.path.samefile(source, target):
            raise RuntimeError('Unexpected cached part: ' + str(target))
    destination = root / 'verified-install'
    result = root / 'installer-engine-result.txt'
    with (root / 'full-install.log').open('w', encoding='utf-8') as log:
        subprocess.run([str(assets / 'II-Setup.exe'), '--test-install', str(manifest_path),
                        str(destination), 'http://127.0.0.1:9', str(result)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    if result.read_text(encoding='utf-8-sig').strip() != 'OK':
        raise RuntimeError('Full installation did not complete')
    python = destination / 'runtime/python.exe'
    subprocess.run([str(python), '-I', '-c',
                    "import torch, av; from ii.config import ROOT; from ii.comfy import load_settings; "
                    "from ii.wan import status; c=load_settings(); assert status(c)['ready']; "
                    "assert c['python']==str(ROOT/'runtime/python.exe'); "
                    "assert c['install_path']==str(ROOT/'ComfyUI'); "
                    "print('Installed runtime and Wan paths: OK; CUDA:',torch.cuda.is_available())"], check=True)
    (root / 'full-install-result.txt').write_text('OK', encoding='utf-8')
    print('Full installer verification: OK', destination, flush=True)


if __name__ == '__main__':
    main()

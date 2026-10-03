"""Check release hashes and actual unauthenticated public downloads."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import time

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repo', default='dydosux/II-downloads')
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    release = json.loads(subprocess.check_output(
        ['gh', 'api', f'repos/{args.repo}/releases/tags/{args.tag}'], text=True, encoding='utf-8'))
    assert not release['draft'] and release['tag_name'] == args.tag
    manifest = json.loads((root/'release/manifest.json').read_text())
    expected = manifest['parts'] + json.loads((root/'release-extras.json').read_text())
    assets = {a['name']: a for a in release['assets']}
    assert not any(n.startswith('transfer-') for n in assets), 'Staging blocks remain'
    for item in expected:
        actual = assets[item['name']]
        assert actual['size'] == item['size']
        assert actual['digest'] == 'sha256:' + item['sha256']
        assert actual['state'] == 'uploaded'

    def check(item):
        url = assets[item['name']]['browser_download_url']
        path = root/'release'/item['name']
        headers = {'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache'}
        if item['size'] < 1024*1024:
            with requests.get(url, headers=headers, stream=True, timeout=(20,60)) as response:
                assert response.status_code == 200, (item['name'], response.status_code)
                body = response.raw.read(item['size'] + 1)
                assert len(body) == item['size']
                assert hashlib.sha256(body).hexdigest() == item['sha256']
        else:
            for start in (0, item['size'] - 65536):
                end = start + 65535
                with requests.get(url, headers={**headers, 'Range': f'bytes={start}-{end}'},
                                  stream=True, timeout=(20,60)) as response:
                    assert response.status_code == 206, (item['name'], response.status_code)
                    assert response.headers['Content-Range'] == f'bytes {start}-{end}/{item["size"]}'
                    body = response.raw.read(65537)
                    with path.open('rb') as local:
                        local.seek(start)
                        assert body == local.read(65536), item['name']
        print('Public download verified:', item['name'], flush=True)
        return {'name': item['name'], 'size': item['size'], 'sha256': item['sha256'], 'public': True}

    with ThreadPoolExecutor(max_workers=4) as pool:
        checked = list(pool.map(check, expected))
    latest = f'https://github.com/{args.repo}/releases/latest/download/II-Setup.exe'
    with requests.get(latest, stream=True, timeout=(20,60)) as response:
        assert response.status_code == 200
        assert hashlib.sha256(response.raw.read(1024*1024)).hexdigest() == assets['II-Setup.exe']['digest'][7:]
    report = {'tag': args.tag, 'draft': False, 'checked_at': time.time(),
              'release': release['html_url'], 'latest_installer': latest, 'assets': checked}
    (root/'public-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Public release verification: OK', release['html_url'], flush=True)


if __name__ == '__main__':
    main()

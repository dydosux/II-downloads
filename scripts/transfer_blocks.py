"""Send independently resumable blocks; GitHub Actions reconstructs pinned assets."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

ROOT = Path('G:/II-installer-build-20260927')
REPO = 'dydosux/II-downloads'
RELEASE = 397629500


def api(args):
    return subprocess.check_output(['gh', *args], text=True, encoding='utf-8')


def prepare():
    folder = ROOT / 'transfer-blocks'
    folder.mkdir(exist_ok=True)
    manifest = json.loads((ROOT/'release/manifest.json').read_text())
    result = []
    for part in manifest['parts']:
        blocks = []
        with (ROOT/'release'/part['name']).open('rb') as source:
            index = 0
            while data := source.read(32*1024*1024):
                index += 1
                name = f"transfer-{part['name']}-{index:03d}"
                path = folder/name
                digest = hashlib.sha256(data).hexdigest()
                if not path.exists(): path.write_bytes(data)
                blocks.append(dict(name=name, size=len(data), sha256=digest))
        result.append(dict(**part, blocks=blocks))
    target = ROOT/'transfer-manifest.json'
    target.write_text(json.dumps(dict(repo=REPO, release=RELEASE, parts=result),indent=2))
    print('Prepared', sum(len(p['blocks']) for p in result), 'blocks', flush=True)


def upload():
    manifest = json.loads((ROOT/'transfer-manifest.json').read_text())
    expected = [b for p in manifest['parts'] for b in p['blocks']]
    pages=json.loads(api(['api',f'repos/{REPO}/releases/{RELEASE}/assets?per_page=100','--paginate','--slurp']))
    existing = {a['name']:a for page in pages for a in page}
    for old in existing.values():
        if old['name'].startswith('transfer-') and old.get('state')!='uploaded':
            api(['api','--method','DELETE',f"repos/{REPO}/releases/assets/{old['id']}"])
    pending = [b for b in expected if existing.get(b['name'],{}).get('digest') != 'sha256:'+b['sha256']]
    token = api(['auth','token']).strip()
    completed = sum(b['size'] for b in expected if b not in pending)
    total = sum(b['size'] for b in expected)
    lock = threading.Lock()
    start=time.monotonic()
    def send(block):
        nonlocal completed
        path=ROOT/'transfer-blocks'/block['name']
        for attempt in range(8):
            try:
                with path.open('rb') as data:
                    request=urllib.request.Request(f"https://uploads.github.com/repos/{REPO}/releases/{RELEASE}/assets?name={block['name']}",
                        data=data, headers={'Authorization':'Bearer '+token,'Content-Type':'application/octet-stream',
                        'Content-Length':str(block['size']),'User-Agent':'II-Block-Publisher'},method='POST')
                    with urllib.request.urlopen(request,timeout=120) as response: asset=json.load(response)
                if asset.get('size')!=block['size'] or asset.get('digest')!='sha256:'+block['sha256']:
                    raise RuntimeError('Uploaded block checksum mismatch')
                break
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 422:
                    assets=json.loads(api(['api',f'repos/{REPO}/releases/{RELEASE}/assets?per_page=100','--paginate','--slurp']))
                    assets=[a for page in assets for a in page]
                    old=next((a for a in assets if a['name']==block['name']),None)
                    if old and old.get('digest')=='sha256:'+block['sha256']: break
                    if old: api(['api','--method','DELETE',f"repos/{REPO}/releases/assets/{old['id']}"])
                if attempt==7: raise
                print('Retry',block['name'],type(exc).__name__,flush=True)
                time.sleep(min(20,2*(attempt+1)))
        with lock:
            completed+=block['size']
            status=dict(bytes=completed,total=total,percent=round(100*completed/total,1),seconds=round(time.monotonic()-start))
            (ROOT/'transfer-progress.json').write_text(json.dumps(status))
            print('Verified',block['name'],status['percent'],'%',flush=True)
    with ThreadPoolExecutor(max_workers=64) as pool:
        for task in as_completed([pool.submit(send,b) for b in pending]): task.result()
    print('ALL BLOCKS UPLOADED',flush=True)
    api(['workflow','run','assemble-release.yml','--repo',REPO])
    print('ASSEMBLY WORKFLOW STARTED',flush=True)


if __name__ == '__main__':
    prepare() if sys.argv[1]=='prepare' else upload()

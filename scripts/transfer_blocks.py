"""Send independently resumable blocks; GitHub Actions reconstructs pinned assets."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

ROOT = None
REPO = None
RELEASE = None
TAG = None
WORKERS = 32


def api(args):
    return subprocess.check_output(['gh', *args], text=True, encoding='utf-8')


def prepare():
    if (ROOT/'full-install-result.txt').read_text(encoding='utf-8-sig').strip() != 'OK':
        raise RuntimeError('Full installation verification has not succeeded')
    manifest = json.loads((ROOT/'release/manifest.json').read_text())
    result = []
    for part in manifest['parts']:
        blocks = []
        with (ROOT/'release'/part['name']).open('rb') as source:
            index = 0
            while data := source.read(32*1024*1024):
                index += 1
                name = f"transfer-{part['name']}-{index:03d}"
                digest = hashlib.sha256(data).hexdigest()
                blocks.append(dict(name=name, size=len(data), sha256=digest, offset=(index-1)*32*1024*1024))
        result.append(dict(**part, blocks=blocks))
    target = ROOT/'transfer-manifest.json'
    target.write_text(json.dumps(dict(repo=REPO, release=RELEASE, tag=TAG, parts=result),indent=2))
    print('Prepared', sum(len(p['blocks']) for p in result), 'blocks', flush=True)


def upload():
    if (ROOT/'full-install-result.txt').read_text(encoding='utf-8-sig').strip() != 'OK':
        raise RuntimeError('Full installation verification has not succeeded')
    release = json.loads(api(['api', f'repos/{REPO}/releases/{RELEASE}']))
    if not release['draft'] or release['tag_name'] != TAG:
        raise RuntimeError('Uploads require the matching draft release')
    manifest = json.loads((ROOT/'transfer-manifest.json').read_text())
    expected = [dict(b, source=p['name']) for p in manifest['parts'] for b in p['blocks']]
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
        path=ROOT/'release'/block['source']
        for attempt in range(8):
            try:
                with path.open('rb') as data:
                    data.seek(block['offset'])
                    content = data.read(block['size'])
                    if hashlib.sha256(content).hexdigest() != block['sha256']:
                        raise RuntimeError('Local payload changed')
                    request=urllib.request.Request(f"https://uploads.github.com/repos/{REPO}/releases/{RELEASE}/assets?name={block['name']}",
                        data=io.BytesIO(content), headers={'Authorization':'Bearer '+token,'Content-Type':'application/octet-stream',
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
                print('Retry',block['name'],type(exc).__name__,str(getattr(exc, 'reason', exc)),flush=True)
                time.sleep(min(20,2*(attempt+1)))
        with lock:
            completed+=block['size']
            status=dict(bytes=completed,total=total,percent=round(100*completed/total,1),seconds=round(time.monotonic()-start))
            (ROOT/'transfer-progress.json').write_text(json.dumps(status))
            print('Verified',block['name'],status['percent'],'%',flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for task in as_completed([pool.submit(send,b) for b in pending]): task.result()
    print('ALL BLOCKS UPLOADED',flush=True)
    api(['workflow','run','assemble-release.yml','--repo',REPO])
    print('ASSEMBLY WORKFLOW STARTED',flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'upload'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repo', default='dydosux/II-downloads')
    parser.add_argument('--tag', required=True)
    parser.add_argument('--release', type=int, required=True)
    parser.add_argument('--workers', type=int, default=32)
    args = parser.parse_args()
    ROOT, REPO, RELEASE, TAG, WORKERS = args.root.resolve(), args.repo, args.release, args.tag, args.workers
    prepare() if args.action=='prepare' else upload()

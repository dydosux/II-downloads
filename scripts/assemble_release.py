"""Run on GitHub Actions: restore exact installer assets from checked blocks."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True)


def assets(repo, release):
    pages=json.loads(gh('api',f'repos/{repo}/releases/{release}/assets?per_page=100','--paginate','--slurp'))
    return {a['name']:a for page in pages for a in page}


def main():
    manifest=json.loads(Path('transfer-manifest.json').read_text())
    repo,release=manifest['repo'],manifest['release']
    remote=assets(repo,release)
    if sys.argv[1]=='publish':
        expected=manifest['parts']+json.loads(Path('release-extras.json').read_text())
        for item in expected:
            actual=remote.get(item['name'],{})
            if actual.get('size')!=item['size'] or actual.get('digest')!='sha256:'+item['sha256'] or actual.get('state')!='uploaded':
                raise RuntimeError('Missing or corrupt final asset: '+item['name'])
        # Remove staging files while still a draft, so users see only final assets.
        for part in manifest['parts']:
            for block in part['blocks']:
                if block['name'] in remote:
                    gh('api','--method','DELETE',f"repos/{repo}/releases/assets/{remote[block['name']]['id']}")
        gh('release','edit','v2026.09.27','--repo',repo,'--draft=false','--latest')
        print('RELEASE PUBLISHED')
        return
    part=manifest['parts'][int(sys.argv[1])-1]
    existing=remote.get(part['name'],{})
    if existing.get('digest')=='sha256:'+part['sha256'] and existing.get('size')==part['size']:
        print('Already verified',part['name']);return
    folder=Path('blocks');folder.mkdir(exist_ok=True)
    def fetch(block):
        actual=remote[block['name']]
        if actual.get('digest')!='sha256:'+block['sha256'] or actual.get('size')!=block['size']:
            raise RuntimeError('Invalid staging block')
        path=folder/block['name']
        with path.open('wb') as output:
            subprocess.run(['gh','api','-H','Accept: application/octet-stream',f"repos/{repo}/releases/assets/{actual['id']}"],stdout=output,check=True)
        with path.open('rb') as data: digest=hashlib.file_digest(data,'sha256').hexdigest()
        if digest!=block['sha256']:raise RuntimeError('Downloaded block mismatch')
        return path
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fetch,part['blocks']))
    target=Path(part['name']);digest=hashlib.sha256();size=0
    with target.open('wb') as output:
        for block in part['blocks']:
            path=folder/block['name']
            with path.open('rb') as source:
                while data:=source.read(1024*1024):output.write(data);digest.update(data);size+=len(data)
            path.unlink()
    if size!=part['size'] or digest.hexdigest()!=part['sha256']:
        raise RuntimeError('Reconstructed asset mismatch')
    gh('release','upload','v2026.09.27',str(target),'--repo',repo)
    print('Verified and restored',part['name'])


if __name__=='__main__':main()

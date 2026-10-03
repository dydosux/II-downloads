import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import assemble_release as assembly


class AssemblyTests(unittest.TestCase):
    def test_reconstructed_bytes_match_original(self):
        data = b"original immutable payload"
        blocks = [data[:9], data[9:]]
        names = ["block-1", "block-2"]
        digest = lambda b: hashlib.sha256(b).hexdigest()
        entries = [dict(name=n, size=len(b), sha256=digest(b)) for n,b in zip(names, blocks)]
        part = dict(name="payload.001", size=len(data), sha256=digest(data), blocks=entries)
        remote = {entry['name']: dict(id=i, size=entry['size'], digest='sha256:'+entry['sha256']) for i,entry in enumerate(entries)}
        calls=[]
        def download(args, stdout, check):
            stdout.write(blocks[int(args[-1].split('/')[-1])])
        previous=os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                Path('transfer-manifest.json').write_text(json.dumps(dict(repo='owner/repo', release=1, tag='v-test', parts=[part])))
                with patch('sys.argv',['assembly','1']), patch.object(assembly,'assets',return_value=remote), patch.object(assembly.subprocess,'run',side_effect=download), patch.object(assembly,'gh',side_effect=lambda *a: calls.append(a)):
                    assembly.main()
                self.assertEqual(Path('payload.001').read_bytes(),data)
                self.assertEqual(calls[0][:2],('release','upload'))
                self.assertEqual(calls[0][2], 'v-test')
            finally:
                os.chdir(previous)

    def test_missing_asset_prevents_publication(self):
        previous=os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                Path('transfer-manifest.json').write_text(json.dumps(dict(repo='owner/repo',release=1,parts=[dict(name='missing',size=1,sha256='abc',blocks=[])])))
                Path('release-extras.json').write_text('[]')
                with patch('sys.argv',['assembly','publish']),patch.object(assembly,'assets',return_value={}),patch.object(assembly,'gh') as gh:
                    with self.assertRaisesRegex(RuntimeError,'Missing or corrupt'):
                        assembly.main()
                    gh.assert_not_called()
            finally:
                os.chdir(previous)


if __name__=='__main__':unittest.main()

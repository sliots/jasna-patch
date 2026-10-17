import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import subprocess
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import ci_release as ci


def source_records(cfg=ci.DEFAULT_CONFIG, counts=(4, 4)):
    result = []
    for (tag, _), count in zip(cfg.versions, counts):
        pattern = cfg.pattern(tag)
        names = [pattern[:-1] + f'.{i:03d}' for i in range(1, count + 1)] if pattern.endswith('*') else [pattern]
        result.append({'repository': cfg.source_repository, 'tag': tag, 'url': cfg.url(tag),
                       'assets': [{'name': name, 'size': 1, 'digest': 'sha256:' + 'a' * 64,
                                   'browser_download_url': 'https://example.invalid/' + name} for name in names]})
    return result


def config_env(cfg):
    return {'PATCH_' + name.upper(): str(value) for name, value in cfg.to_dict().items()}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.out = self.base / 'artifacts'
        self.out.mkdir()
        self.repo = 'acme/patch'
        self.ctx = {'repo': self.repo, 'tag': ci.TAG, 'release_id': 100, 'mode': 'publish'}
        self.env = mock.patch.dict(os.environ, {'PATCH_REPOSITORY': self.repo, 'GITHUB_OUTPUT': '', **config_env(ci.DEFAULT_CONFIG)})
        self.env.start()
        self.addCleanup(self.env.stop)
        ci.write(self.out / 'diff.json', {
            'from': 'v0.11.1', 'to': 'v0.11.2', 'added': ['a'], 'modified': ['b'], 'deleted': ['c'],
        })
        ci.write(self.out / 'source-assets.json', source_records())
        ci.write(self.out / 'verification.json', {
            **{k: True for k in ci.VERIFIED_FLAGS}, 'actual_powershell_apply_exit_code': 0,
            'source_archive_parts_sha256_verified': 8, 'target_files': 3,
        })
        (self.out / f'{ci.TAG}.7z').write_bytes(b'transport fixture, not used as an actual archive')
        ci.checksum_files(self.out)

    def release(self, draft=True, names=None):
        names = names if names is not None else [p.name for p in self.out.iterdir()]
        return {
            'id': 100, 'draft': draft, 'tag_name': ci.TAG, 'html_url': 'https://example.invalid/release',
            'assets': [{'id': i, 'name': name, 'size': (self.out / name).stat().st_size,
                        'digest': 'sha256:' + ci.digest(self.out / name)} for i, name in enumerate(names, 1)],
        }

    def test_published_release_checks_small_metadata_and_skips_build(self):
        target = self.release(draft=False)
        with mock.patch.object(ci, 'gh_json', return_value=[target]) as api, \
             mock.patch.object(ci, 'remote_bytes', side_effect=lambda repo, a: (self.out / a['name']).read_bytes()) as fetch:
            ctx = ci.preflight(self.base / 'work', 'publish', '')
        self.assertEqual(ctx['action'], 'done')
        api.assert_called_once_with('repos/acme/patch/releases?per_page=100&page=1')
        self.assertEqual({call.args[1]['name'] for call in fetch.call_args_list},
                         {'SHA256SUMS.txt', 'verification.json', 'diff.json', 'source-assets.json'})

    def test_upstream_ids_cannot_replace_target_id(self):
        target = self.release(names=[])
        upstream = []
        for i, (version, _) in enumerate(ci.VERSIONS, 1000):
            upstream.append({'id': i, 'tag_name': f'v{version}', 'html_url': 'https://example.invalid',
                             'assets': [{'name': f'jasna-windows-{version}.7z.{n:03d}', 'size': 1,
                                         'digest': 'sha256:' + 'a' * 64,
                                         'browser_download_url': 'https://example.invalid'} for n in range(1, 5)]})
        with mock.patch.object(ci, 'gh_json', side_effect=[[target], *upstream]):
            ctx = ci.preflight(self.base / 'work', 'publish', '')
        self.assertEqual(ctx['release_id'], 100)
        self.assertEqual([r['tag'] for r in ctx['sources']], ['v0.11.1', 'v0.11.2'])
        with mock.patch.object(ci, 'gh_json', return_value=self.release(draft=False)) as api, \
             mock.patch.object(ci, 'run') as command:
            ci.publish(self.base, ctx)
        self.assertEqual(api.call_args.args[0], 'repos/acme/patch/releases/100')
        command.assert_not_called()

    def test_partial_matching_upload_is_resumed_without_reupload(self):
        first = self.release(names=['diff.json'])
        all_assets = self.release()
        published = self.release(draft=False)
        with mock.patch.object(ci, 'gh_json', side_effect=[first, all_assets, published]), \
             mock.patch.object(ci, 'run') as command:
            ci.publish(self.base, self.ctx)
        upload = command.call_args_list[0].args[0]
        self.assertEqual(upload[:4], ['gh', 'release', 'upload', ci.TAG])
        paths = [p.name for p in upload if isinstance(p, pathlib.Path)]
        self.assertNotIn('diff.json', paths)
        self.assertEqual(len(paths), 4)
        edit = command.call_args_list[1].args[0]
        self.assertEqual(edit[edit.index('--tag') + 1], ci.TAG)

    def test_mismatched_remote_asset_is_not_overwritten(self):
        target = self.release(names=['diff.json'])
        target['assets'][0]['digest'] = 'sha256:' + '0' * 64
        with mock.patch.object(ci, 'gh_json', return_value=target), mock.patch.object(ci, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
                ci.publish(self.base, self.ctx)
        command.assert_not_called()

    def test_corrupt_bundle_is_rejected_before_network_mutation(self):
        (self.out / f'{ci.TAG}.7z').write_bytes(b'corrupt')
        with mock.patch.object(ci, 'gh_json') as api, mock.patch.object(ci, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                ci.publish(self.base, self.ctx)
        api.assert_not_called()
        command.assert_not_called()

    def test_target_tag_change_is_rejected(self):
        target = self.release(names=[])
        target['tag_name'] = 'other-release'
        with mock.patch.object(ci, 'gh_json', return_value=target), mock.patch.object(ci, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'tag changed'):
                ci.publish(self.base, self.ctx)
        command.assert_not_called()

    def test_recovery_requires_verified_report(self):
        report = json.loads((self.out / 'verification.json').read_text())
        report['actual_powershell_apply_exit_code'] = 1
        ci.write(self.out / 'verification.json', report)
        ci.checksum_files(self.out)
        with self.assertRaisesRegex(ValueError, 'successful full validation'):
            ci.local_bundle(self.out)

    def test_recovery_does_not_rebuild_sources(self):
        target = self.release(names=['diff.json'])
        with mock.patch.object(ci, 'gh_json', side_effect=[[target], {'path': ci.WORKFLOW_PATH, 'status': 'completed'}]) as api:
            ctx = ci.preflight(self.base / 'work', 'publish', '123')
        self.assertEqual(ctx['action'], 'recover')
        self.assertEqual(api.call_args.args[0], 'repos/acme/patch/actions/runs/123')
        with mock.patch.object(ci, 'run') as command:
            ci.recover(self.base, ctx)
        self.assertEqual(command.call_args.args[0][:3], ['gh', 'run', 'download'])

    def test_target_on_later_release_page_is_found(self):
        target = self.release(names=[])
        with mock.patch.object(ci, 'gh_json', side_effect=[[{'tag_name': 'other'}] * 100, [target]]) as api, \
             mock.patch.object(ci, 'source_metadata', return_value=[]):
            ctx = ci.preflight(self.base / 'work', 'publish', '')
        self.assertEqual(ctx['release_id'], 100)
        self.assertEqual(api.call_args.args[0], 'repos/acme/patch/releases?per_page=100&page=2')

    def test_bad_download_checksum_is_never_installed_or_retried(self):
        import io
        data = b'untrusted bytes'
        path = self.base / 'tool.tar.xz'
        with mock.patch.object(ci.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as request:
            with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                ci.download('https://example.invalid/tool', path, '0' * 64, len(data))
        request.assert_called_once()
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name + '.partial').exists())

    def test_certificate_failure_is_not_retried(self):
        error = ci.urllib.error.URLError(ci.ssl.SSLCertVerificationError('certificate verification failed'))
        with mock.patch.object(ci.urllib.request, 'urlopen', side_effect=error) as request:
            with self.assertRaises(ci.urllib.error.URLError):
                ci.download('https://example.invalid/tool', self.base / 'tool.tar.xz', '0' * 64)
        request.assert_called_once()

    def test_unsafe_and_duplicate_checksums_rejected(self):
        for text in [f'{"a"*64}  ../escape\n', f'{"a"*64}  x\n{"a"*64}  x\n']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                ci.parse_checksums(text.encode())

    def test_volume_boundary_and_reassembly(self):
        for size in (31, 32, 33, 96, 97):
            with self.subTest(size=size):
                archive = self.base / f'archive-{size}.7z'
                data = bytes(range(size))
                archive.write_bytes(data)
                parts = ci.split_archive(archive, limit=32)
                self.assertEqual(b''.join(p.read_bytes() for p in parts), data)
                self.assertTrue(all(p.stat().st_size <= 32 for p in parts))
                self.assertEqual(len(parts), (size + 31) // 32)
                self.assertEqual(archive.exists(), size <= 32)


class FunctionalPatchTests(unittest.TestCase):
    @unittest.skipUnless((shutil.which('7zz') or shutil.which('7z')) and shutil.which('pwsh'),
                         '7-Zip and PowerShell required for the real updater fixture')
    def test_real_archive_and_updater_restore_target_and_reject_bad_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for version in ('old', 'new'):
                tree = root / version
                tree.mkdir()
                (tree / 'jasna.exe').write_bytes(version.encode())
                (tree / 'unchanged.bin').write_bytes(b'\x00unchanged\xff')
            (root / 'old' / 'deleted.txt').write_text('obsolete')
            (root / 'new' / 'added.txt').write_text('added')
            ctx = {'repo': 'fixture/patch', 'sources': source_records()}
            ci.build(root, ctx)
            (root / 'tools').mkdir()
            (root / 'tools' / '7zz').symlink_to(shutil.which('7zz') or shutil.which('7z'))
            with mock.patch.dict(os.environ, {'PATCH_COMPRESSION_LEVEL': '3'}):
                ci.pack(root)
                ci.validate(root, ctx)
            self.assertEqual(ci.scan(root / 'old'), ci.scan(root / 'new'))
            self.assertEqual(json.loads((root / 'artifacts' / 'diff.json').read_text())['deleted'], ['deleted.txt'])
            ci.local_bundle(root / 'artifacts')
            # Restore a deliberately modified baseline: the actual script must fail before writes.
            for p in (root / 'old').iterdir():
                p.unlink()
            (root / 'old' / 'jasna.exe').write_bytes(b'corrupt baseline')
            (root / 'old' / 'unchanged.bin').write_bytes(b'\x00unchanged\xff')
            (root / 'old' / 'deleted.txt').write_text('obsolete')
            before = ci.scan(root / 'old')
            result = subprocess.run(['pwsh', '-NoLogo', '-NoProfile', '-File',
                                     str(root / 'verified-patch' / 'Apply-Patch.ps1'),
                                     '-InstallDir', str(root / 'old')], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(ci.scan(root / 'old'), before)


if __name__ == '__main__':
    unittest.main()

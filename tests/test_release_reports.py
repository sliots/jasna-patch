"""Exercise the Git Data API contract against real local Git objects and refs."""
import json
import pathlib
import subprocess
import tempfile
import unittest
import urllib.parse
from unittest import mock

import test_ci_release as release_tests
from test_ci_release import TEST_CONFIG

ci = release_tests.ci


class LocalGitAPI:
    def __init__(self, root):
        self.root = root
        self.mutations = []
        self.ref_reads = 0
        self.before_ref_read = None

    def git(self, *args, data=None):
        return subprocess.check_output(['git', *args], cwd=self.root, input=data)

    def __call__(self, path, *, method=None, payload=None):
        endpoint = urllib.parse.unquote(path.split('/git/', 1)[1])
        if method:
            self.mutations.append((endpoint, method, payload))
        if endpoint.startswith('matching-refs/'):
            self.ref_reads += 1
            if self.before_ref_read:
                self.before_ref_read(self.ref_reads)
            prefix = 'refs/' + endpoint.removeprefix('matching-refs/')
            lines = self.git('for-each-ref', '--format=%(refname) %(objecttype) %(objectname)', prefix).decode().splitlines()
            return [{'ref': ref, 'object': {'type': kind, 'sha': sha}}
                    for ref, kind, sha in (line.split() for line in lines)]
        if endpoint.startswith('commits/'):
            sha = endpoint.split('/', 1)[1]
            return {'sha': sha, 'tree': {'sha': self.git('show', '-s', '--format=%T', sha).decode().strip()}}
        if endpoint.startswith('trees/'):
            tree = []
            for entry in self.git('ls-tree', '-z', endpoint.split('/', 1)[1]).split(b'\0'):
                if entry:
                    fields, name = entry.decode().split('\t')
                    mode, kind, sha = fields.split()
                    tree.append({'path': name, 'mode': mode, 'type': kind, 'sha': sha})
            return {'tree': tree, 'truncated': False}
        if endpoint == 'trees' and method == 'POST':
            entries = {}
            if 'base_tree' in payload:
                entries = {e['path']: e for e in self(f'repos/fixture/patch/git/trees/{payload["base_tree"]}')['tree']}
            for entry in payload['tree']:
                entry = dict(entry)
                if 'content' in entry:
                    entry['sha'] = self.git('hash-object', '-w', '--stdin', data=entry.pop('content').encode()).decode().strip()
                entries[entry['path']] = entry
            listing = ''.join(f'{e["mode"]} {e["type"]} {e["sha"]}\t{e["path"]}\n' for e in entries.values())
            return {'sha': self.git('mktree', data=listing.encode()).decode().strip()}
        if endpoint == 'commits' and method == 'POST':
            parents = [arg for parent in payload['parents'] for arg in ('-p', parent)]
            return {'sha': self.git('commit-tree', payload['tree'], *parents, data=payload['message'].encode()).decode().strip()}
        if endpoint.startswith('refs/tags/') and method == 'PATCH':
            ref = endpoint
            old = self.git('rev-parse', ref).decode().strip()
            assert payload['force'] is False
            self.git('merge-base', '--is-ancestor', old, payload['sha'])
            self.git('update-ref', ref, payload['sha'], old)
            return {}
        if endpoint == 'refs' and method == 'POST':
            self.git('update-ref', payload['ref'], payload['sha'], '0' * 40)
            return {}
        raise AssertionError((endpoint, method, payload))


class ReportSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)
        self.api = LocalGitAPI(self.root)
        self.api.git('init', '-q', '-b', 'main')
        self.api.git('config', 'user.name', 'Report fixture')
        self.api.git('config', 'user.email', 'fixture@example.invalid')
        (self.root / 'README.md').write_text('Generic instructions\n')
        (self.root / 'reports').mkdir()
        (self.root / 'reports' / 'diff.json').write_text('{"from":"stale","to":"stale"}\n')
        (self.root / 'reports' / 'obsolete.json').write_text('{}\n')
        self.api.git('add', '.')
        self.api.git('commit', '-qm', 'Generic source with obsolete reports')
        self.base = self.api.git('rev-parse', 'HEAD').decode().strip()
        self.cfg = TEST_CONFIG
        self.reports = {
            'diff.json': json.dumps({'from': self.cfg.old_tag, 'to': self.cfg.new_tag}).encode() + b'\n',
            'source-assets.json': b'[]\n', 'verification.json': b'{"fixture":true}\n',
            'SHA256SUMS.txt': b'fixture: exact Release bytes\n',
        }
        self.patch = mock.patch.object(ci, 'gh_json', side_effect=self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def snapshot(self):
        return ci.snapshot_reports('fixture/patch', self.cfg, self.reports, self.base)

    def test_new_tag_contains_exact_reports_and_preserves_main(self):
        commit = self.snapshot()
        self.assertEqual(self.api.git('rev-parse', f'refs/tags/{self.cfg.tag}').decode().strip(), commit)
        self.assertEqual(self.api.git('rev-parse', 'main').decode().strip(), self.base)
        self.assertEqual(set(self.api.git('ls-tree', '--name-only', commit + ':reports').decode().splitlines()), set(self.reports))
        for name, data in self.reports.items():
            self.assertEqual(self.api.git('show', f'{commit}:reports/{name}'), data)
        self.assertEqual(self.api.git('show', commit + ':README.md'), b'Generic instructions\n')
        self.assertTrue(all(not endpoint.startswith('refs/heads/') for endpoint, _, _ in self.api.mutations))

    def test_existing_wrong_tag_is_fast_forwarded_and_repeat_is_noop(self):
        self.api.git('tag', self.cfg.tag)
        commit = self.snapshot()
        self.api.git('merge-base', '--is-ancestor', self.base, commit)
        mutations = len(self.api.mutations)
        self.assertEqual(self.snapshot(), commit)
        self.assertEqual(len(self.api.mutations), mutations)
        self.assertEqual(self.api.mutations[-1][1:], ('PATCH', {'sha': commit, 'force': False}))

    def test_concurrent_tag_move_is_preserved(self):
        self.api.git('tag', self.cfg.tag)
        tree = self.api.git('show', '-s', '--format=%T', self.base).decode().strip()
        other = self.api.git('commit-tree', tree, '-p', self.base, data=b'Concurrent change').decode().strip()
        def move_on_second_read(count):
            if count == 2:
                self.api.git('update-ref', f'refs/tags/{self.cfg.tag}', other, self.base)
        self.api.before_ref_read = move_on_second_read
        with self.assertRaisesRegex(ValueError, 'tag changed'):
            self.snapshot()
        self.assertEqual(self.api.git('rev-parse', f'refs/tags/{self.cfg.tag}').decode().strip(), other)
        self.assertFalse(any(endpoint.startswith('refs') for endpoint, _, _ in self.api.mutations))

    def test_incomplete_report_set_fails_before_api_calls(self):
        self.reports.pop('verification.json')
        with self.assertRaisesRegex(ValueError, 'complete report set'):
            self.snapshot()
        self.assertEqual(self.api.ref_reads, 0)


class PublishedReportSyncTests(unittest.TestCase):
    setUp = release_tests.ReleaseTests.setUp
    release = release_tests.ReleaseTests.release

    def test_existing_release_syncs_reports_without_archive_or_upload(self):
        target = self.release(draft=False)
        for archive in self.out.glob('*.7z'):
            archive.unlink()
        ctx = {**self.ctx, 'action': 'reports'}
        synced = {**target, 'target_commitish': 'c' * 40}
        with mock.patch.object(ci, 'gh_json', side_effect=[target, synced]), \
             mock.patch.object(ci, 'remote_bytes', side_effect=lambda repo, a: (self.out / a['name']).read_bytes()), \
             mock.patch.object(ci, 'snapshot_reports', return_value='c' * 40) as snapshot, \
             mock.patch.object(ci, 'run') as command:
            ci.publish(self.base, ctx)
        reports = snapshot.call_args.args[2]
        self.assertEqual(set(reports), ci.REQUIRED_REPORTS | {'SHA256SUMS.txt'})
        self.assertEqual(reports['diff.json'], (self.out / 'diff.json').read_bytes())
        command.assert_called_once_with(['gh', 'release', 'edit', self.ctx['tag'], '--repo', self.repo,
                                         '--tag', self.ctx['tag'], '--target', 'c' * 40])

    def test_corrupt_published_metadata_cannot_move_tag(self):
        target = self.release(draft=False)
        (self.out / 'diff.json').write_text('{"from":"wrong","to":"wrong"}')
        asset_names = {str(a['id']): a['name'] for a in target['assets']}
        with mock.patch.object(ci, 'gh_json', return_value=target), \
             mock.patch.object(ci.subprocess, 'check_output', side_effect=lambda args:
                               (self.out / asset_names[args[2].split('/')[-1]]).read_bytes()), \
             mock.patch.object(ci, 'snapshot_reports') as snapshot, \
             mock.patch.object(ci, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                ci.publish(self.base, {**self.ctx, 'action': 'reports'})
        snapshot.assert_not_called()
        command.assert_not_called()

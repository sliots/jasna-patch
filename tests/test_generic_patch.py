import json
import os
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import ci_release as ci
from patch_config import PatchConfig
import test_ci_release as release_tests
from test_ci_release import config_env, source_records


class GenericConfigTests(unittest.TestCase):
    def test_version_inputs_are_required_without_historical_fallback(self):
        for values in ({}, {'PATCH_OLD_TAG': 'baseline'},
                       {'PATCH_OLD_TAG': '', 'PATCH_NEW_TAG': 'target'}):
            with self.subTest(values=values), mock.patch.dict(os.environ, values, clear=True):
                with self.assertRaisesRegex(ValueError, 'PATCH_OLD_TAG and PATCH_NEW_TAG are required'):
                    PatchConfig.from_env()

    def test_config_round_trip_and_auto_tag(self):
        cfg = PatchConfig(source_repository='other/widget', old_tag='v1.2', new_tag='v2.5',
                          asset_pattern='widget-{version}.zip', entry_file='bin/widget.exe', volume_size_mb=500)
        self.assertEqual(cfg.tag, 'v2.5-v1.2.patch')
        self.assertEqual(cfg.pattern(cfg.new_tag), 'widget-2.5.zip')
        self.assertEqual(cfg.volume_bytes, 500_000_000)
        self.assertEqual(PatchConfig(**cfg.to_dict()).to_dict(), cfg.to_dict())
        with mock.patch.dict(os.environ, config_env(cfg)):
            self.assertEqual(PatchConfig.from_env().to_dict(), cfg.to_dict())

    def test_tag_slash_is_encoded_and_archive_name_is_flat(self):
        cfg = PatchConfig(old_tag='release/1.0', new_tag='release/2.0', asset_pattern='windows-*.zip')
        self.assertIn('release%2F2.0', cfg.url(cfg.new_tag))
        self.assertNotIn('/', cfg.archive_stem)

    def test_invalid_parameters_are_rejected(self):
        cases = [{'entry_file': '../escape.exe'}, {'new_tag': 'bad\ntag'}, {'source_repository': '../repo'},
                 {'asset_pattern': '{unknown}.zip'}, {'asset_pattern': '{tag.__class__}.zip'},
                 {'volume_size_mb': 2001}, {'old_tag': release_tests.TEST_CONFIG.new_tag}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                PatchConfig(**{**release_tests.TEST_CONFIG.to_dict(), **values})

    def test_single_archives_and_different_volume_counts(self):
        cfg = PatchConfig(old_tag='v1', new_tag='v2', asset_pattern='app-{version}.7z*')
        for count in (1, 3, 5):
            records = source_records(cfg, counts=(count, count))
            self.assertEqual(len(ci.select_assets(records[0]['assets'], cfg.pattern(cfg.old_tag))), count)
        zip_cfg = PatchConfig(old_tag='v1', new_tag='v2', asset_pattern='app-{tag}.zip')
        assets = source_records(zip_cfg, counts=(1, 1))[0]['assets']
        self.assertEqual(ci.select_assets(assets, zip_cfg.pattern(zip_cfg.old_tag)), assets)

    def test_gaps_multiple_families_and_missing_digests_fail(self):
        assets = source_records()[0]['assets']
        for invalid in [assets[:1] + assets[2:], assets + [{**assets[0], 'name': 'other.7z.001'}],
                        [{**assets[0], 'digest': None}], [{**assets[0], 'name': '../escape.7z'}]]:
            with self.subTest(names=[a['name'] for a in invalid]), self.assertRaises(ValueError):
                ci.select_assets(invalid, '*.7z*')

    def test_archive_paths_and_links_checked_before_extraction(self):
        self.assertEqual(ci.inspect_archive('\n----------\nPath = bin/widget.exe\nSize = 42\n\n'), 42)
        for record in ['Path = ../escape\nSize = 42', 'Path = /absolute\nSize = 42',
                       'Path = link\nSize = 0\nSymbolic Link = /outside']:
            with self.subTest(record=record), self.assertRaises(ValueError):
                ci.inspect_archive('\n----------\n' + record + '\n\n')

    def test_missing_draft_is_created_after_source_checks(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {'PATCH_REPOSITORY': 'acme/patch', **config_env(release_tests.TEST_CONFIG)}), \
             mock.patch.object(ci, 'gh_json', side_effect=[[], [{'id': 123, 'draft': True, 'tag_name': release_tests.TEST_CONFIG.tag, 'assets': []}]]), \
             mock.patch.object(ci, 'source_metadata', return_value=source_records()), \
             mock.patch.object(ci, 'run') as run:
            ctx = ci.preflight(pathlib.Path(directory) / 'work', 'publish', '')
        self.assertEqual(ctx['release_id'], 123)
        self.assertEqual(run.call_args.args[0][:4], ['gh', 'release', 'create', release_tests.TEST_CONFIG.tag])

    def test_validate_mode_does_not_create_missing_release(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {'PATCH_REPOSITORY': 'acme/patch', **config_env(release_tests.TEST_CONFIG)}), \
             mock.patch.object(ci, 'gh_json', return_value=[]), \
             mock.patch.object(ci, 'source_metadata', return_value=source_records()), \
             mock.patch.object(ci, 'run') as run:
            ctx = ci.preflight(pathlib.Path(directory) / 'work', 'validate', '')
        self.assertIsNone(ctx['release_id'])
        self.assertEqual(ctx['action'], 'build')
        run.assert_not_called()


class GenericBindingTests(unittest.TestCase):
    setUp = release_tests.ReleaseTests.setUp

    def test_recovery_parameters_must_match(self):
        report = json.loads((self.out / 'verification.json').read_text())
        report['parameters'] = release_tests.TEST_CONFIG.to_dict()
        ci.write(self.out / 'verification.json', report)
        ci.checksum_files(self.out)
        with self.assertRaisesRegex(ValueError, 'parameters do not match'):
            ci.local_bundle(self.out, PatchConfig(**{**release_tests.TEST_CONFIG.to_dict(), 'volume_size_mb': 500}))

    def test_same_version_tags_cannot_reuse_other_upstream(self):
        with self.assertRaisesRegex(ValueError, 'upstream identity mismatch'):
            ci.local_bundle(self.out, PatchConfig(**{**release_tests.TEST_CONFIG.to_dict(), 'source_repository': 'other/widget'}))


class GenericFunctionalTests(unittest.TestCase):
    @unittest.skipUnless((shutil.which('7zz') or shutil.which('7z')) and shutil.which('pwsh'),
                         '7-Zip and PowerShell required')
    def test_real_three_and_five_volume_source_archives(self):
        cfg = PatchConfig(source_repository='other/widget', old_tag='1.0', new_tag='2.0',
                          asset_pattern='widget-{version}.7z*', entry_file='widget.exe')
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'tools').mkdir()
            (root / 'tools' / '7zz').symlink_to(shutil.which('7zz') or shutil.which('7z'))
            sources = []
            for (tag, label), size in zip(cfg.versions, (1_200_000, 2_200_000)):
                folder = root / ('fixture-' + label)
                folder.mkdir()
                (folder / 'widget.exe').write_bytes(random.Random(size).randbytes(size))
                archive = root / f'widget-{tag}.7z'
                subprocess.run([str(root / 'tools' / '7zz'), 'a', '-t7z', '-mx=0', str(archive), '.'],
                               cwd=folder, stdout=subprocess.DEVNULL, check=True)
                parts = ci.split_archive(archive, 500_000)
                sources.append({'repository': cfg.source_repository, 'tag': tag, 'url': cfg.url(tag),
                                'assets': [{'name': p.name, 'size': p.stat().st_size,
                                            'digest': 'sha256:' + ci.digest(p),
                                            'browser_download_url': p.as_uri()} for p in parts]})
            self.assertEqual([len(s['assets']) for s in sources], [3, 5])
            ctx = {'repo': 'fixture/patch', 'config': cfg.to_dict(), 'sources': sources}
            ci.extract_version(root, ctx, 'old')
            ci.extract_version(root, ctx, 'new')
            ci.build(root, ctx)
            ci.pack(root, ctx)
            ci.validate(root, ctx)
            self.assertEqual(ci.scan(root / 'old'), ci.scan(root / 'new'))

    @unittest.skipUnless((shutil.which('7zz') or shutil.which('7z')) and shutil.which('pwsh'),
                         '7-Zip and PowerShell required')
    def test_data_only_patch_without_executable_marker(self):
        cfg = PatchConfig(source_repository='other/data', old_tag='2025.1', new_tag='2026.2',
                          asset_pattern='data-{tag}.zip', entry_file='')
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for label in ('old', 'new'):
                (root / label).mkdir()
                (root / label / 'data.txt').write_text(label)
            (root / 'tools').mkdir()
            (root / 'tools' / '7zz').symlink_to(shutil.which('7zz') or shutil.which('7z'))
            ctx = {'repo': 'fixture/data', 'config': cfg.to_dict(), 'sources': source_records(cfg, (1, 1))}
            ci.build(root, ctx)
            ci.pack(root, ctx)
            ci.validate(root, ctx)
            self.assertEqual(ci.scan(root / 'old'), ci.scan(root / 'new'))

    @unittest.skipUnless((shutil.which('7zz') or shutil.which('7z')) and shutil.which('pwsh'),
                         '7-Zip and PowerShell required')
    def test_zip_wrappers_other_executable_versions_and_real_split_update(self):
        cfg = PatchConfig(source_repository='other/widget', old_tag='v1.0.0', new_tag='v2.0.0',
                          asset_pattern='widget-{version}.zip', release_tag='widget-upgrade.patch',
                          entry_file='bin/widget.exe', volume_size_mb=1)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'tools').mkdir()
            (root / 'tools' / '7zz').symlink_to(shutil.which('7zz') or shutil.which('7z'))
            sources = []
            for tag, label in cfg.versions:
                source = root / f'fixture-{label}'
                app = source / f'Widget-{tag}'
                (app / 'bin').mkdir(parents=True)
                exe = b'old' if label == 'old' else b'new' + random.Random(123).randbytes(2_200_000)
                (app / 'bin' / 'widget.exe').write_bytes(exe)
                (app / 'shared.txt').write_text('unchanged')
                (app / ('deleted.txt' if label == 'old' else 'added.txt')).write_text(label)
                archive = root / cfg.pattern(tag)
                subprocess.run([str(root / 'tools' / '7zz'), 'a', '-tzip', str(archive), '.'], cwd=source,
                               stdout=subprocess.DEVNULL, check=True)
                sources.append({'repository': cfg.source_repository, 'tag': tag, 'url': cfg.url(tag),
                                'assets': [{'name': archive.name, 'size': archive.stat().st_size,
                                            'digest': 'sha256:' + ci.digest(archive),
                                            'browser_download_url': archive.as_uri()}]})
            ctx = {'repo': 'fixture/patch', 'tag': cfg.tag, 'release_id': None, 'mode': 'validate',
                   'action': 'build', 'config': cfg.to_dict(), 'sources': sources}
            ci.write(root / 'context.json', ctx)
            ci.extract_version(root, ctx, 'old')
            ci.extract_version(root, ctx, 'new')
            self.assertEqual(ci.application_root(root, ctx, 'old').name, 'Widget-v1.0.0')
            # Exercise real CLI phases too, including canonical saved config comparisons.
            env = {**os.environ, **config_env(cfg), 'PATCH_REPOSITORY': 'fixture/patch', 'PATCH_COMPRESSION_LEVEL': '3'}
            for phase in ('build', 'pack', 'validate'):
                subprocess.run([sys.executable, str(pathlib.Path(ci.__file__)), '--work-dir', str(root),
                                '--phase', phase], env=env, check=True, stdout=subprocess.DEVNULL)
            parts = sorted((root / 'artifacts').glob('*.7z.*'))
            self.assertGreater(len(parts), 1)
            self.assertTrue(all(p.stat().st_size <= 1_000_000 for p in parts))
            expected = ci.scan(ci.application_root(root, ctx, 'new'))
            self.assertEqual(ci.scan(ci.application_root(root, ctx, 'old')), expected)
            report = json.loads((root / 'artifacts' / 'verification.json').read_text())
            self.assertEqual(report['source_archive_parts_sha256_verified'], 2)
            self.assertEqual(report['parameters'], cfg.to_dict())
            self.assertEqual(json.loads((root / 'artifacts' / 'diff.json').read_text())['from'], 'v1.0.0')
            self.assertNotIn('Jasna', (root / 'verified-patch' / 'README.txt').read_text())
            self.assertIn('other/widget', ci.release_notes(root / 'artifacts', cfg))
            ci.local_bundle(root / 'artifacts', cfg)

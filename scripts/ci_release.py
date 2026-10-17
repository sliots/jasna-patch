#!/usr/bin/env python3
"""Build, validate and publish a Windows patch in separately resumable CI phases."""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

from build_patch import scan, write

TAG = 'v0.11.2-v0.11.1.patch'
VERSIONS = [('0.11.1', 'old'), ('0.11.2', 'new')]
SEVEN_ZIP_URL = 'https://github.com/ip7z/7zip/releases/download/26.04/7z2604-linux-x64.tar.xz'
SEVEN_ZIP_SHA256 = 'fc0327ba27e89bd086cf426dff17d77de582953cdbbc10a6576540a06853ffcd'
LIMIT = 1_000_000_000
MIN_FREE = 38_000_000_000
WORKFLOW_PATH = '.github/workflows/publish-patch.yml'
REQUIRED_REPORTS = {'diff.json', 'source-assets.json', 'verification.json'}
VERIFIED_FLAGS = (
    'source_archives_extraction_crc_verified',
    'patch_archive_extraction_crc_verified',
    'updated_file_set_and_sha256_exactly_match_target',
)


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def gh_json(path):
    return json.loads(subprocess.check_output(['gh', 'api', path]))


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def download(url, path, sha256, size=None):
    """Hash while downloading; never accept partial or unchecked downloads."""
    partial = path.with_name(path.name + '.partial')
    for attempt in range(3):
        try:
            h = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=120) as source, partial.open('wb') as target:
                while chunk := source.read(8 * 1024 * 1024):
                    target.write(chunk)
                    h.update(chunk)
            if size is not None and partial.stat().st_size != size:
                raise ValueError(f'Incorrect download size: {path.name}')
            if h.hexdigest() != sha256:
                raise ValueError(f'SHA256 mismatch: {path.name}')
            partial.replace(path)
            print('SHA256 verified:', path.name, flush=True)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # Certificate errors, bad credentials and policy denials need diagnosis.
            if (isinstance(getattr(exc, 'reason', None), ssl.SSLCertVerificationError)
                    or (isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504))) or attempt == 2:
                raise
            print(f'Transient download failure for {path.name}: {type(exc).__name__}; retrying', flush=True)
            time.sleep(2 ** attempt)
        finally:
            partial.unlink(missing_ok=True)


def checksum_files(out):
    (out / 'SHA256SUMS.txt').write_text(''.join(
        f'{digest(p)}  {p.name}\n' for p in sorted(out.iterdir())
        if p.is_file() and p.name != 'SHA256SUMS.txt'), encoding='utf-8')


def parse_checksums(data):
    result = {}
    for line in data.decode('utf-8').splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] in result or match[2] in ('.', '..', 'SHA256SUMS.txt'):
            raise ValueError('Malformed, unsafe or duplicate checksum entry')
        result[match[2]] = match[1]
    if not result:
        raise ValueError('Empty checksum list')
    return result


def check_names(names):
    archives = sorted(n for n in names if n.startswith(TAG + '.7z'))
    if not archives or names != REQUIRED_REPORTS | {'SHA256SUMS.txt'} | set(archives):
        raise ValueError('Unexpected or missing artifact files')
    if archives != [TAG + '.7z']:
        expected = [f'{TAG}.7z.{i:03d}' for i in range(1, len(archives) + 1)]
        if archives != expected:
            raise ValueError('Archive volumes must be consecutive from .001')
    return archives


def check_report(report):
    if (any(report.get(k) is not True for k in VERIFIED_FLAGS)
            or report.get('actual_powershell_apply_exit_code') != 0
            or report.get('source_archive_parts_sha256_verified') != 8
            or type(report.get('target_files')) is not int
            or report['target_files'] <= 0):
        raise ValueError('The bundle has no successful full validation report')


def local_bundle(out):
    entries = list(out.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in entries):
        raise ValueError('Artifact bundle may contain only regular files')
    names = {p.name for p in entries}
    check_names(names)
    checksums = parse_checksums((out / 'SHA256SUMS.txt').read_bytes())
    if set(checksums) != names - {'SHA256SUMS.txt'}:
        raise ValueError('Checksum file set mismatch')
    for name, sha in checksums.items():
        if digest(out / name) != sha:
            raise ValueError(f'Artifact checksum mismatch: {name}')
    report = json.loads((out / 'verification.json').read_text())
    check_report(report)
    diff = json.loads((out / 'diff.json').read_text())
    if (diff.get('from'), diff.get('to')) != ('v0.11.1', 'v0.11.2'):
        raise ValueError('Artifact version pair mismatch')
    return {p.name: {'size': p.stat().st_size, 'digest': 'sha256:' + digest(p)} for p in entries}


def remote_bytes(repo, asset):
    data = subprocess.check_output([
        'gh', 'api', f'repos/{repo}/releases/assets/{asset["id"]}',
        '-H', 'Accept: application/octet-stream',
    ])
    if len(data) != asset['size'] or 'sha256:' + hashlib.sha256(data).hexdigest() != asset.get('digest'):
        raise ValueError(f'Remote asset checksum mismatch: {asset["name"]}')
    return data


def published_is_complete(repo, release):
    """Read only small metadata files; no archive download or regeneration."""
    assets = {a['name']: a for a in release['assets']}
    check_names(set(assets))
    if len(assets) != len(release['assets']):
        raise ValueError('Duplicate remote asset name')
    checksums = parse_checksums(remote_bytes(repo, assets['SHA256SUMS.txt']))
    if set(checksums) != set(assets) - {'SHA256SUMS.txt'}:
        raise ValueError('Published checksum file set mismatch')
    for name, sha in checksums.items():
        if assets[name]['size'] <= 0 or assets[name].get('digest') != 'sha256:' + sha:
            raise ValueError(f'Published asset mismatch: {name}')
    check_report(json.loads(remote_bytes(repo, assets['verification.json'])))
    diff = json.loads(remote_bytes(repo, assets['diff.json']))
    if (diff.get('from'), diff.get('to')) != ('v0.11.1', 'v0.11.2'):
        raise ValueError('Published version pair mismatch')


def source_metadata():
    result = []
    for version, _ in VERSIONS:
        upstream = gh_json(f'repos/Kruk2/jasna/releases/tags/v{version}')
        assets = sorted((a for a in upstream['assets']
                         if a['name'].startswith(f'jasna-windows-{version}.7z.')), key=lambda a: a['name'])
        if [a['name'].rsplit('.', 1)[-1] for a in assets] != ['001', '002', '003', '004']:
            raise ValueError('Unexpected upstream Windows volume list')
        if any(not re.fullmatch(r'sha256:[a-f0-9]{64}', a.get('digest') or '') for a in assets):
            raise ValueError('Upstream SHA256 is required')
        result.append({'tag': upstream['tag_name'], 'url': upstream['html_url'],
                       'assets': [{k: a[k] for k in ['name', 'size', 'digest', 'browser_download_url']}
                                  for a in assets]})
    return result


def github_output(**values):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            for key, value in values.items():
                f.write(f'{key}={value}\n')


def preflight(root, mode, recovery_run_id):
    if mode not in ('publish', 'validate'):
        raise ValueError('Mode must be publish or validate')
    repo = os.environ['PATCH_REPOSITORY']
    # Paginate: old target drafts may be beyond the first 30 releases.
    matches = []
    page = 1
    while True:
        releases = gh_json(f'repos/{repo}/releases?per_page=100&page={page}')
        matches.extend(r for r in releases if r['tag_name'] == TAG)
        if len(releases) < 100:
            break
        page += 1
    if len(matches) != 1:
        raise ValueError('Exactly one target Release is required; create its draft before publishing')
    target = matches[0]
    ctx = {'repo': repo, 'tag': TAG, 'release_id': target['id'], 'mode': mode}
    if mode == 'publish' and not target['draft']:
        published_is_complete(repo, target)
        ctx['action'] = 'done'
        print('Already published and all asset digests match; skipping rebuild:', target['html_url'], flush=True)
    elif recovery_run_id:
        if not recovery_run_id.isdecimal():
            raise ValueError('Recovery run ID must be numeric')
        previous = gh_json(f'repos/{repo}/actions/runs/{recovery_run_id}')
        if previous.get('path') != WORKFLOW_PATH or previous['status'] != 'completed':
            raise ValueError('Recovery must use a completed run of this repository workflow')
        ctx.update(action='recover', recovery_run_id=recovery_run_id)
    else:
        if mode == 'publish' and target['assets']:
            raise ValueError('Draft has assets; supply recovery_run_id to resume the verified bundle')
        ctx.update(action='build', sources=source_metadata())
    root.mkdir(parents=True, exist_ok=False)
    write(root / 'context.json', ctx)
    github_output(action=ctx['action'])
    return ctx


def seven_zip(root):
    return root / 'tools' / '7zz'


def install_tools(root):
    tools = root / 'tools'
    tools.mkdir()
    archive = tools / '7zip.tar.xz'
    download(SEVEN_ZIP_URL, archive, SEVEN_ZIP_SHA256)
    with tarfile.open(archive) as tf:
        tf.extractall(tools, filter='data')
    if not shutil.which('pwsh'):
        raise RuntimeError('PowerShell is required for actual patch validation')


def extract_version(root, ctx, target):
    if shutil.disk_usage(root).free < (MIN_FREE if target == 'old' else 24_000_000_000):
        raise RuntimeError('Not enough free space for source archives and extraction')
    source = ctx['sources'][0 if target == 'old' else 1]
    downloads = root / f'download-{target}'
    downloads.mkdir()
    def get_asset(asset):
        download(asset['browser_download_url'], downloads / asset['name'],
                 asset['digest'].split(':', 1)[1], asset['size'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(get_asset, source['assets']))
    run([seven_zip(root), 'x', downloads / source['assets'][0]['name'],
         f'-o{root / target}', '-y', '-bsp0', '-mmt=2'])
    shutil.rmtree(downloads)


def split_archive(archive, limit=LIMIT):
    if archive.stat().st_size <= limit:
        return [archive]
    parts = []
    with archive.open('rb') as source:
        while chunk := source.read(min(limit, 8 * 1024 * 1024)):
            part = pathlib.Path(f'{archive}.{len(parts) + 1:03d}')
            with part.open('wb') as target:
                remaining = limit
                while chunk:
                    target.write(chunk)
                    remaining -= len(chunk)
                    if remaining == 0:
                        break
                    chunk = source.read(min(remaining, 8 * 1024 * 1024))
            parts.append(part)
    archive.unlink()
    return parts


def build(root, ctx):
    stage = root / 'stage'
    run(['python3', pathlib.Path(__file__).with_name('build_patch.py'), root / 'old', root / 'new', stage])
    write(stage / 'source-assets.json', ctx['sources'])


def pack(root):
    out = root / 'artifacts'
    out.mkdir()
    packed = out / f'{TAG}.7z'
    level = os.environ.get('PATCH_COMPRESSION_LEVEL', '3')
    if level not in ('3', '5'):
        raise ValueError('Compression level must be 3 or 5')
    dictionary = '16m' if level == '3' else '64m'
    run([seven_zip(root), 'a', '-t7z', f'-mx={level}', f'-md={dictionary}',
         '-mmt=2', '-bsp0', packed, '.'], cwd=root / 'stage')
    split_archive(packed)


def validate(root, ctx):
    out = root / 'artifacts'
    archives = sorted(out.glob('*.7z*'))
    unpacked = root / 'verified-patch'
    run([seven_zip(root), 'x', archives[0], f'-o{unpacked}', '-y', '-bsp0'])
    run(['pwsh', '-NoLogo', '-NoProfile', '-File', unpacked / 'Apply-Patch.ps1', '-InstallDir', root / 'old'])
    expected = json.loads((unpacked / 'new-manifest.json').read_text())
    # Keep independent complete file-set and hash comparisons after the real updater.
    if scan(root / 'old') != expected or scan(root / 'new') != expected:
        raise RuntimeError('Updated file set and SHA256 must exactly match upstream v0.11.2')
    for name in ['diff.json', 'source-assets.json']:
        shutil.copyfile(root / 'stage' / name, out / name)
    write(out / 'verification.json', {
        'validated_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'source_archive_parts_sha256_verified': 8,
        'source_archives_extraction_crc_verified': True,
        'patch_archive_extraction_crc_verified': True,
        'actual_powershell_apply_exit_code': 0,
        'updated_file_set_and_sha256_exactly_match_target': True,
        'target_files': len(expected), 'target_bytes': sum(x['size'] for x in expected.values()),
        'validation_platform': 'Linux x64; Windows application/GPU not launched',
        'actions_run_url': f"https://github.com/{ctx['repo']}/actions/runs/{os.environ.get('GITHUB_RUN_ID', 'local')}",
        'compression_level': int(os.environ.get('PATCH_COMPRESSION_LEVEL', '3')),
        'archives': [{'name': p.name, 'size': p.stat().st_size, 'sha256': digest(p)} for p in archives],
    })
    checksum_files(out)
    local_bundle(out)


def recover(root, ctx):
    run(['gh', 'run', 'download', ctx['recovery_run_id'], '--repo', ctx['repo'],
         '--name', 'patch-build-output', '--dir', root / 'artifacts'])
    local_bundle(root / 'artifacts')
    print('Recovered and SHA256-verified the previously validated bundle', flush=True)


def check_remote_assets(assets, expected, complete):
    received = {a['name']: a for a in assets}
    if len(received) != len(assets) or set(received) - set(expected):
        raise ValueError('Unexpected remote assets; refusing to overwrite them')
    for name, asset in received.items():
        if any(asset.get(k) != expected[name][k] for k in ('size', 'digest')):
            raise ValueError(f'Remote asset mismatch; refusing to overwrite: {name}')
    if complete and set(received) != set(expected):
        raise ValueError('Remote asset list is incomplete')
    return received


def release_notes(out):
    diff = json.loads((out / 'diff.json').read_text())
    report = json.loads((out / 'verification.json').read_text())
    sizes = ', '.join(f'{p.name}: {p.stat().st_size:,} bytes' for p in sorted(out.glob('*.7z*')))
    return f'''Jasna Windows **v0.11.1 → v0.11.2** 文件级差异补丁，非上游官方发布。

新增 {len(diff['added'])} 个、修改 {len(diff['modified'])} 个、删除 {len(diff['deleted'])} 个文件。

压缩附件：{sizes}。超过 1,000,000,000 字节时分卷。

关闭 Jasna 并备份旧版安装目录。将补丁解压到独立目录，在 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\\Apply-Patch.ps1 -InstallDir "C:\\Jasna"
```

安装目录应直接包含 jasna.exe；分卷须全部下载并从 .001 解压。脚本先校验全量旧版文件，再覆盖新增/修改文件、删除指定旧文件，最后校验全量新版文件。用户额外文件保留；旧版官方文件被修改时拒绝更新。失败时恢复备份，没有自动回滚。

已从最终压缩包实际执行 PowerShell 更新，更新结果与新版原始解压目录的 {report['target_files']:,} 个文件集合、大小和 SHA-256 完全一致。上游八个分卷和远端发布附件均经过 SHA-256 校验。未启动 Windows 应用或测试 GPU 功能。

详见 verification.json、diff.json、source-assets.json 和 SHA256SUMS.txt；原组件许可证见包内 upstream-notices/。

上游：[v0.11.1](https://github.com/Kruk2/jasna/releases/tag/v0.11.1)、[v0.11.2](https://github.com/Kruk2/jasna/releases/tag/v0.11.2)。
'''


def publish(root, ctx):
    out = root / 'artifacts'
    expected = local_bundle(out)
    # The target identity comes only from preflight, never from upstream metadata.
    endpoint = f"repos/{ctx['repo']}/releases/{ctx['release_id']}"
    target = gh_json(endpoint)
    if target['tag_name'] != TAG:
        raise ValueError('Target Release tag changed during the build')
    received = check_remote_assets(target['assets'], expected, complete=not target['draft'])
    if not target['draft']:
        print('Identical bundle already published; no changes needed', flush=True)
        return
    missing = [out / name for name in sorted(set(expected) - set(received))]
    if missing:
        run(['gh', 'release', 'upload', TAG, *missing, '--repo', ctx['repo']])
    remote = gh_json(endpoint)
    check_remote_assets(remote['assets'], expected, complete=True)
    notes = root / 'release-notes.md'
    notes.write_text(release_notes(out), encoding='utf-8')
    run(['gh', 'release', 'edit', TAG, '--repo', ctx['repo'], '--tag', TAG,
         '--notes-file', notes, '--draft=false'])
    published = gh_json(endpoint)
    if published['draft'] or published['tag_name'] != TAG:
        raise RuntimeError('Release publication or tag verification failed')
    check_remote_assets(published['assets'], expected, complete=True)
    print('Published:', published['html_url'], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', type=pathlib.Path, required=True)
    parser.add_argument('--phase', choices=['preflight', 'tools', 'extract-old', 'extract-new',
                                           'build', 'pack', 'validate', 'recover', 'publish'], required=True)
    args = parser.parse_args()
    root = args.work_dir.resolve()
    start = time.monotonic()
    if args.phase == 'preflight':
        preflight(root, os.environ.get('PATCH_MODE', 'publish'), os.environ.get('PATCH_RECOVERY_RUN', ''))
    else:
        ctx = json.loads((root / 'context.json').read_text())
        if ctx['repo'] != os.environ['PATCH_REPOSITORY'] or ctx['tag'] != TAG:
            raise ValueError('Work directory context does not match this repository/tag')
        if ctx['action'] == 'done':
            return
        if args.phase == 'tools':
            install_tools(root)
        elif args.phase.startswith('extract-'):
            extract_version(root, ctx, args.phase.split('-', 1)[1])
        elif args.phase == 'build':
            build(root, ctx)
        elif args.phase == 'pack':
            pack(root)
        elif args.phase == 'validate':
            validate(root, ctx)
        elif args.phase == 'recover':
            recover(root, ctx)
        elif args.phase == 'publish':
            if ctx['mode'] != 'publish':
                raise ValueError('Validate-only mode cannot publish')
            publish(root, ctx)
    seconds = time.monotonic() - start
    print(f'{args.phase}: completed in {seconds:.1f}s', flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(f'- `{args.phase}`: {seconds:.1f} seconds\n')


if __name__ == '__main__':
    main()

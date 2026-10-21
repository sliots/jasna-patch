#!/usr/bin/env python3
"""Build, validate and publish a Windows patch in separately resumable CI phases."""
import argparse
import concurrent.futures
import datetime
import fnmatch
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
import urllib.parse

from build_patch import scan, write
from patch_config import PatchConfig, config_for, validate_relative

SEVEN_ZIP_URL = 'https://github.com/ip7z/7zip/releases/download/26.04/7z2604-linux-x64.tar.xz'
SEVEN_ZIP_SHA256 = 'fc0327ba27e89bd086cf426dff17d77de582953cdbbc10a6576540a06853ffcd'
LIMIT = 1_000_000_000
WORKFLOW_PATH = '.github/workflows/publish-patch.yml'
REQUIRED_REPORTS = {'diff.json', 'source-assets.json', 'verification.json'}
VERIFIED_FLAGS = (
    'source_archives_extraction_crc_verified',
    'patch_archive_extraction_crc_verified',
    'updated_file_set_and_sha256_exactly_match_target',
)


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def gh_json(path, *, method=None, payload=None):
    args = ['gh', 'api', path]
    if method:
        args += ['--method', method]
    if payload is not None:
        args += ['--input', '-']
    return json.loads(subprocess.check_output(
        args, input=json.dumps(payload) if payload is not None else None, text=True))


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


def check_names(names, cfg):
    stem = cfg.archive_stem
    archives = sorted(n for n in names if n.startswith(stem + '.7z'))
    if not archives or names != REQUIRED_REPORTS | {'SHA256SUMS.txt'} | set(archives):
        raise ValueError('Unexpected or missing artifact files')
    if archives != [stem + '.7z']:
        expected = [f'{stem}.7z.{i:03d}' for i in range(1, len(archives) + 1)]
        if archives != expected:
            raise ValueError('Archive volumes must be consecutive from .001')
    return archives


def check_report(report):
    if (any(report.get(k) is not True for k in VERIFIED_FLAGS)
            or report.get('actual_powershell_apply_exit_code') != 0
            or type(report.get('source_archive_parts_sha256_verified')) is not int
            or report['source_archive_parts_sha256_verified'] <= 0
            or type(report.get('target_files')) is not int
            or report['target_files'] <= 0):
        raise ValueError('The bundle has no successful full validation report')


def local_bundle(out, cfg):
    entries = list(out.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in entries):
        raise ValueError('Artifact bundle may contain only regular files')
    names = {p.name for p in entries}
    check_names(names, cfg)
    checksums = parse_checksums((out / 'SHA256SUMS.txt').read_bytes())
    if set(checksums) != names - {'SHA256SUMS.txt'}:
        raise ValueError('Checksum file set mismatch')
    for name, sha in checksums.items():
        if digest(out / name) != sha:
            raise ValueError(f'Artifact checksum mismatch: {name}')
    report = json.loads((out / 'verification.json').read_text())
    diff = json.loads((out / 'diff.json').read_text())
    sources = json.loads((out / 'source-assets.json').read_text())
    check_binding(diff, sources, report, cfg)
    return {p.name: {'size': p.stat().st_size, 'digest': 'sha256:' + digest(p)} for p in entries}


def remote_bytes(repo, asset):
    data = subprocess.check_output([
        'gh', 'api', f'repos/{repo}/releases/assets/{asset["id"]}',
        '-H', 'Accept: application/octet-stream',
    ])
    if len(data) != asset['size'] or 'sha256:' + hashlib.sha256(data).hexdigest() != asset.get('digest'):
        raise ValueError(f'Remote asset checksum mismatch: {asset["name"]}')
    return data


def published_is_complete(repo, release, cfg):
    """Read only small metadata files; no archive download or regeneration."""
    assets = {a['name']: a for a in release['assets']}
    check_names(set(assets), cfg)
    if len(assets) != len(release['assets']):
        raise ValueError('Duplicate remote asset name')
    reports = {'SHA256SUMS.txt': remote_bytes(repo, assets['SHA256SUMS.txt'])}
    checksums = parse_checksums(reports['SHA256SUMS.txt'])
    if set(checksums) != set(assets) - {'SHA256SUMS.txt'}:
        raise ValueError('Published checksum file set mismatch')
    for name, sha in checksums.items():
        if assets[name]['size'] <= 0 or assets[name].get('digest') != 'sha256:' + sha:
            raise ValueError(f'Published asset mismatch: {name}')
    reports.update((name, remote_bytes(repo, assets[name])) for name in sorted(REQUIRED_REPORTS))
    check_binding(json.loads(reports['diff.json']), json.loads(reports['source-assets.json']),
                  json.loads(reports['verification.json']), cfg)
    return reports


def select_assets(assets, pattern):
    selected = sorted((a for a in assets if fnmatch.fnmatchcase(a['name'], pattern)), key=lambda a: a['name'])
    if not selected:
        raise ValueError(f'No assets match {pattern!r}')
    if any(not a['name'] or any(c in a['name'] for c in '/\\:\x00') or a['name'] in ('.', '..') for a in selected):
        raise ValueError('Asset names must be plain filenames')
    if len(selected) == 1 and selected[0]['name'].lower().endswith(('.7z', '.zip')):
        pass
    else:
        volumes = [re.fullmatch(r'(.+\.(?:7z|zip))\.(\d{3,})', a['name'], re.IGNORECASE) for a in selected]
        if (any(m is None for m in volumes) or len({m[1] for m in volumes}) != 1
                or [int(m[2]) for m in volumes] != list(range(1, len(selected) + 1))):
            raise ValueError('Select exactly one 7z/zip or one consecutive volume set starting at .001')
    if any(not re.fullmatch(r'sha256:[a-f0-9]{64}', a.get('digest') or '')
           or type(a.get('size')) is not int or a['size'] <= 0 for a in selected):
        raise ValueError('Every selected asset requires a size and GitHub SHA256 digest')
    return selected


def check_binding(diff, sources, report, cfg):
    check_report(report)
    if (diff.get('from'), diff.get('to')) != (cfg.old_tag, cfg.new_tag):
        raise ValueError('Artifact version pair mismatch')
    if diff.get('entry_file', cfg.entry_file) != cfg.entry_file:
        raise ValueError('Artifact entry file mismatch')
    if diff.get('source_repository', cfg.source_repository).casefold() != cfg.source_repository.casefold():
        raise ValueError('Artifact source repository mismatch')
    if 'parameters' in report and report['parameters'] != cfg.to_dict():
        raise ValueError('Recovered artifact parameters do not match the requested patch')
    if len(sources) != 2:
        raise ValueError('Exactly two source release records are required')
    count = 0
    for source, (tag, _) in zip(sources, cfg.versions):
        expected_prefix = f'https://github.com/{cfg.source_repository}/releases/tag/'
        url = source.get('url', '')
        if (source.get('tag') != tag or not url[:len(expected_prefix)].casefold() == expected_prefix.casefold()
                or urllib.parse.unquote(url[len(expected_prefix):]) != tag):
            raise ValueError('Artifact upstream identity mismatch')
        selected = select_assets(source['assets'], cfg.pattern(tag))
        if len(selected) != len(source['assets']):
            raise ValueError('Artifact asset selection mismatch')
        count += len(selected)
    if report['source_archive_parts_sha256_verified'] != count:
        raise ValueError('Source archive validation count mismatch')


def find_release(repo, tag):
    matches = []
    page = 1
    while True:
        releases = gh_json(f'repos/{repo}/releases?per_page=100&page={page}')
        matches.extend(r for r in releases if r['tag_name'] == tag)
        if len(releases) < 100:
            break
        page += 1
    if len(matches) > 1:
        raise ValueError('More than one Release uses the requested tag')
    return matches[0] if matches else None


def require_space(root, size, operation):
    if shutil.disk_usage(root).free < size:
        raise RuntimeError(f'Not enough free space for {operation}: need {size:,} bytes')


def inspect_archive(listing):
    if '\n----------\n' not in listing.replace('\r\n', '\n'):
        raise ValueError('Unable to inspect archive contents')
    files = listing.replace('\r\n', '\n').split('\n----------\n', 1)[1]
    sizes = []
    for block in files.split('\n\n'):
        values = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
        if 'Path' not in values:
            continue
        path = values['Path'].replace('\\', '/')
        validate_relative(path)
        if values.get('Symbolic Link') or values.get('Hard Link'):
            raise ValueError('Linked archive entries are not supported')
        if 'Size' not in values or not values['Size'].isdigit():
            raise ValueError('Archive entry size is unavailable')
        sizes.append(int(values['Size']))
    if not sizes:
        raise ValueError('Archive contains no entries')
    return sum(sizes)


def content_root(root, entry_file):
    if not entry_file:
        return root
    candidate = root
    while not (candidate / entry_file).is_file():
        children = list(candidate.iterdir())
        if len(children) != 1 or not children[0].is_dir() or children[0].is_symlink():
            raise ValueError(f'Cannot locate {entry_file!r}; choose the entry_file relative to the application root')
        candidate = children[0]
    return candidate


def application_root(root, ctx, target):
    paths = root / 'roots.json'
    if paths.exists():
        relative = json.loads(paths.read_text())[target]
        validate_relative(relative)
        result = root / relative
        if not result.resolve().is_relative_to((root / target).resolve()):
            raise ValueError('Application root escapes the extracted release')
        return result
    return content_root(root / target, config_for(ctx).entry_file)


def source_metadata(cfg):
    result = []
    for tag, _ in cfg.versions:
        upstream = gh_json(f'repos/{cfg.source_repository}/releases/tags/{urllib.parse.quote(tag, safe="")}')
        assets = select_assets(upstream['assets'], cfg.pattern(tag))
        result.append({'repository': cfg.source_repository, 'tag': upstream['tag_name'], 'url': upstream['html_url'],
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
    cfg = PatchConfig.from_env()
    repo = os.environ['PATCH_REPOSITORY']
    target = find_release(repo, cfg.tag)
    sources = None
    if target is None and mode == 'publish':
        sources = source_metadata(cfg)
        run(['gh', 'release', 'create', cfg.tag, '--repo', repo, '--draft',
             '--target', os.environ.get('GITHUB_SHA', 'main'), '--title', cfg.tag,
             '--notes', 'Patch preparation pending validation.'])
        target = find_release(repo, cfg.tag)
        if target is None:
            raise RuntimeError('Could not confirm the newly created draft')
    ctx = {'repo': repo, 'tag': cfg.tag, 'release_id': target['id'] if target else None,
           'mode': mode, 'config': cfg.to_dict(), 'source_commit': os.environ.get('GITHUB_SHA')}
    if mode == 'publish' and not target['draft']:
        published_is_complete(repo, target, cfg)
        ctx['action'] = 'reports'
        print('Published assets verified; synchronize tagged reports without rebuilding:', target['html_url'], flush=True)
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
        sources = sources if sources is not None else source_metadata(cfg)
        compressed = sum(a['size'] for source in sources for a in source['assets'])
        # An initial estimate only; actual unpacked sizes are checked before extraction.
        ctx.update(action='build', sources=sources,
                   required_free_bytes=max(1_000_000_000, compressed * 5 // 2 + 2_000_000_000))
    root.mkdir(parents=True, exist_ok=False)
    write(root / 'context.json', ctx)
    github_output(action=ctx['action'], disk_bytes=ctx.get('required_free_bytes', 0))
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
    cfg = config_for(ctx)
    source = ctx['sources'][0 if target == 'old' else 1]
    downloads = root / f'download-{target}'
    downloads.mkdir()
    require_space(root, sum(a['size'] for a in source['assets']) + 1_000_000_000, 'source download')
    def get_asset(asset):
        download(asset['browser_download_url'], downloads / asset['name'],
                 asset['digest'].split(':', 1)[1], asset['size'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(get_asset, source['assets']))
    archive = downloads / source['assets'][0]['name']
    listing = subprocess.check_output([str(seven_zip(root)), 'l', '-slt', str(archive)], text=True)
    unpacked_bytes = inspect_archive(listing)
    require_space(root, unpacked_bytes + 1_000_000_000, 'archive extraction')
    run([seven_zip(root), 'x', archive, f'-o{root / target}', '-y', '-bsp0', '-mmt=2'])
    shutil.rmtree(downloads)
    # Resolve the optional version-specific wrapper folder once and save its relative root.
    app_root = content_root(root / target, cfg.entry_file)
    paths = root / 'roots.json'
    roots = json.loads(paths.read_text()) if paths.exists() else {}
    roots[target] = app_root.relative_to(root).as_posix()
    write(paths, roots)


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
    cfg = config_for(ctx)
    stage = root / 'stage'
    run(['python3', pathlib.Path(__file__).with_name('build_patch.py'),
         application_root(root, ctx, 'old'), application_root(root, ctx, 'new'), stage,
         '--old-tag', cfg.old_tag, '--new-tag', cfg.new_tag,
         '--source-repository', cfg.source_repository, '--entry-file', cfg.entry_file])
    write(stage / 'source-assets.json', ctx['sources'])


def pack(root, ctx):
    cfg = config_for(ctx)
    out = root / 'artifacts'
    out.mkdir()
    packed = out / f'{cfg.archive_stem}.7z'
    level = os.environ.get('PATCH_COMPRESSION_LEVEL', '3')
    if level not in ('3', '5'):
        raise ValueError('Compression level must be 3 or 5')
    dictionary = '16m' if level == '3' else '64m'
    require_space(root, sum(p.stat().st_size for p in (root / 'stage').rglob('*') if p.is_file())
                  + 1_000_000_000, 'patch compression')
    run([seven_zip(root), 'a', '-t7z', f'-mx={level}', f'-md={dictionary}',
         '-mmt=2', '-bsp0', packed, '.'], cwd=root / 'stage')
    if packed.stat().st_size > cfg.volume_bytes:
        require_space(root, packed.stat().st_size, 'archive splitting')
    split_archive(packed, cfg.volume_bytes)


def validate(root, ctx):
    cfg = config_for(ctx)
    out = root / 'artifacts'
    archives = sorted(out.glob('*.7z*'))
    unpacked = root / 'verified-patch'
    run([seven_zip(root), 'x', archives[0], f'-o{unpacked}', '-y', '-bsp0'])
    old_root, new_root = (application_root(root, ctx, target) for target in ('old', 'new'))
    run(['pwsh', '-NoLogo', '-NoProfile', '-File', unpacked / 'Apply-Patch.ps1', '-InstallDir', old_root])
    expected = json.loads((unpacked / 'new-manifest.json').read_text())
    if scan(old_root) != expected or scan(new_root) != expected:
        raise RuntimeError('Updated file set and SHA256 must exactly match the target release')
    for name in ['diff.json', 'source-assets.json']:
        shutil.copyfile(root / 'stage' / name, out / name)
    write(out / 'verification.json', {
        'validated_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'source_archive_parts_sha256_verified': sum(len(s['assets']) for s in ctx['sources']),
        'source_archives_extraction_crc_verified': True,
        'patch_archive_extraction_crc_verified': True,
        'actual_powershell_apply_exit_code': 0,
        'updated_file_set_and_sha256_exactly_match_target': True,
        'target_files': len(expected), 'target_bytes': sum(x['size'] for x in expected.values()),
        'validation_platform': 'Linux x64; application/GPU not launched',
        'actions_run_url': f"https://github.com/{ctx['repo']}/actions/runs/{os.environ.get('GITHUB_RUN_ID', 'local')}",
        'compression_level': int(os.environ.get('PATCH_COMPRESSION_LEVEL', '3')),
        'parameters': cfg.to_dict(),
        'archives': [{'name': p.name, 'size': p.stat().st_size, 'sha256': digest(p)} for p in archives],
    })
    checksum_files(out)
    local_bundle(out, cfg)


def recover(root, ctx):
    run(['gh', 'run', 'download', ctx['recovery_run_id'], '--repo', ctx['repo'],
         '--name', 'patch-build-output', '--dir', root / 'artifacts'])
    local_bundle(root / 'artifacts', config_for(ctx))
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


def tag_reference(repo, tag):
    refs = gh_json(f'repos/{repo}/git/matching-refs/tags/{urllib.parse.quote(tag, safe="")}')
    matches = [ref for ref in refs if ref['ref'] == f'refs/tags/{tag}']
    if len(matches) > 1:
        raise ValueError('Ambiguous release tag')
    return matches[0] if matches else None


def referenced_commit(repo, ref):
    obj = ref['object']
    for _ in range(8):
        if obj['type'] == 'commit':
            return obj['sha']
        if obj['type'] != 'tag':
            break
        obj = gh_json(f'repos/{repo}/git/tags/{obj["sha"]}')['object']
    raise ValueError('Release tag must resolve to a commit')


def tree_entries(repo, sha):
    tree = gh_json(f'repos/{repo}/git/trees/{sha}')
    if tree.get('truncated'):
        raise ValueError('Incomplete Git tree response')
    return {entry['path']: entry for entry in tree['tree']}


def report_blob_ids(reports):
    if set(reports) != REQUIRED_REPORTS | {'SHA256SUMS.txt'}:
        raise ValueError('A release snapshot requires the complete report set')
    return {name: hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            for name, data in reports.items()}


def reports_match_tree(repo, root_tree, expected):
    reports = tree_entries(repo, root_tree).get('reports')
    if not reports or reports['type'] != 'tree':
        return False
    entries = tree_entries(repo, reports['sha'])
    return set(entries) == set(expected) and all(
        entries[name]['type'] == 'blob' and entries[name]['mode'] == '100644'
        and entries[name]['sha'] == sha for name, sha in expected.items())


def snapshot_reports(repo, cfg, reports, source_commit=None):
    """Tag the source plus byte-identical verified reports, without changing main."""
    expected = report_blob_ids(reports)
    ref = tag_reference(repo, cfg.tag)
    previous = referenced_commit(repo, ref) if ref else None
    if previous:
        commit = gh_json(f'repos/{repo}/git/commits/{previous}')
        if reports_match_tree(repo, commit['tree']['sha'], expected):
            print('Tagged reports already match the verified Release assets', flush=True)
            return previous
    source_commit = source_commit or subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch(r'[a-f0-9]{40}', source_commit):
        raise ValueError('Release snapshot requires an exact source commit SHA')
    base = gh_json(f'repos/{repo}/git/commits/{source_commit}')
    report_tree = gh_json(f'repos/{repo}/git/trees', method='POST', payload={
        'tree': [{'path': name, 'mode': '100644', 'type': 'blob', 'content': data.decode('utf-8')}
                 for name, data in sorted(reports.items())],
    })
    root_tree = gh_json(f'repos/{repo}/git/trees', method='POST', payload={
        'base_tree': base['tree']['sha'],
        'tree': [{'path': 'reports', 'mode': '040000', 'type': 'tree', 'sha': report_tree['sha']}],
    })
    # Include the old tagged commit as a parent so correcting a published tag is
    # a fast-forward too. Never force-update a tag or touch a branch reference.
    parents = list(dict.fromkeys([source_commit] + ([previous] if previous else [])))
    snapshot = gh_json(f'repos/{repo}/git/commits', method='POST', payload={
        'message': f'Record verified release reports for {cfg.tag}', 'tree': root_tree['sha'], 'parents': parents,
    })
    current = tag_reference(repo, cfg.tag)
    if (current['object'] if current else None) != (ref['object'] if ref else None):
        raise ValueError('Release tag changed while preparing its report snapshot')
    if ref:
        gh_json(f'repos/{repo}/git/refs/tags/{urllib.parse.quote(cfg.tag, safe="")}', method='PATCH',
                payload={'sha': snapshot['sha'], 'force': False})
    else:
        gh_json(f'repos/{repo}/git/refs', method='POST',
                payload={'ref': f'refs/tags/{cfg.tag}', 'sha': snapshot['sha']})
    final = tag_reference(repo, cfg.tag)
    if (not final or referenced_commit(repo, final) != snapshot['sha']
            or not reports_match_tree(repo, root_tree['sha'], expected)):
        raise RuntimeError('Tagged release reports do not match the verified assets')
    print('Release reports snapshot:', snapshot['sha'], flush=True)
    return snapshot['sha']


def release_notes(out, cfg):
    diff = json.loads((out / 'diff.json').read_text())
    report = json.loads((out / 'verification.json').read_text())
    sizes = ', '.join(f'{p.name}: {p.stat().st_size:,} bytes' for p in sorted(out.glob('*.7z*')))
    marker = f'安装根目录应包含 `{cfg.entry_file}`。' if cfg.entry_file else '请选择旧版应用的根目录。'
    return f'''**{cfg.old_tag} → {cfg.new_tag}** 文件级差异补丁，来源 `{cfg.source_repository}`，非上游官方发布。

新增 {len(diff['added'])} 个、修改 {len(diff['modified'])} 个、删除 {len(diff['deleted'])} 个文件。

压缩附件：{sizes}。超过 {cfg.volume_bytes:,} 字节时分卷。

关闭应用并备份旧版目录。将补丁解压到独立目录，在 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\\Apply-Patch.ps1 -InstallDir "C:\\Application"
```

{marker} 分卷须全部下载并从 .001 解压。脚本先校验旧版文件，再复制新增/修改文件、删除指定旧文件，最后校验新版文件。用户额外文件保留；旧版文件被修改时拒绝更新。失败时恢复备份，没有自动回滚。

已实际执行更新，结果与新版原始解压目录的 {report['target_files']:,} 个文件集合、大小和 SHA-256 一致。上游 {report['source_archive_parts_sha256_verified']} 个归档附件和远端发布附件均经过 SHA-256 校验。未启动应用或测试 GPU 功能。

详见 verification.json、diff.json、source-assets.json 和 SHA256SUMS.txt；原组件许可证见包内 upstream-notices/。

上游：[旧版]({cfg.url(cfg.old_tag)})、[新版]({cfg.url(cfg.new_tag)})。
'''


def publish(root, ctx):
    out = root / 'artifacts'
    cfg = config_for(ctx)
    reports_only = ctx['action'] == 'reports'
    expected = None if reports_only else local_bundle(out, cfg)
    # The target identity comes only from preflight, never from upstream metadata.
    endpoint = f"repos/{ctx['repo']}/releases/{ctx['release_id']}"
    target = gh_json(endpoint)
    if target['tag_name'] != cfg.tag:
        raise ValueError('Target Release tag changed during the build')
    if reports_only and target['draft']:
        raise ValueError('Published Release became a draft during report synchronization')
    if not target['draft']:
        if expected is not None:
            check_remote_assets(target['assets'], expected, complete=True)
        reports = published_is_complete(ctx['repo'], target, cfg)
        commit = snapshot_reports(ctx['repo'], cfg, reports, ctx.get('source_commit'))
        if target.get('target_commitish') != commit:
            run(['gh', 'release', 'edit', cfg.tag, '--repo', ctx['repo'], '--tag', cfg.tag, '--target', commit])
        confirmed = gh_json(endpoint)
        if confirmed['draft'] or confirmed['tag_name'] != cfg.tag or confirmed['target_commitish'] != commit:
            raise RuntimeError('Release report snapshot binding failed')
        published_is_complete(ctx['repo'], confirmed, cfg)
        print('Published assets preserved; tagged reports synchronized:', confirmed['html_url'], flush=True)
        return
    received = check_remote_assets(target['assets'], expected, complete=False)
    missing = [out / name for name in sorted(set(expected) - set(received))]
    if missing:
        run(['gh', 'release', 'upload', cfg.tag, *missing, '--repo', ctx['repo']])
    remote = gh_json(endpoint)
    check_remote_assets(remote['assets'], expected, complete=True)
    reports = published_is_complete(ctx['repo'], remote, cfg)
    commit = snapshot_reports(ctx['repo'], cfg, reports, ctx.get('source_commit'))
    notes = root / 'release-notes.md'
    notes.write_text(release_notes(out, cfg), encoding='utf-8')
    run(['gh', 'release', 'edit', cfg.tag, '--repo', ctx['repo'], '--tag', cfg.tag,
         '--target', commit, '--notes-file', notes, '--draft=false'])
    published = gh_json(endpoint)
    if published['draft'] or published['tag_name'] != cfg.tag or published['target_commitish'] != commit:
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
        if (ctx['repo'] != os.environ['PATCH_REPOSITORY'] or ctx['tag'] != config_for(ctx).tag
                or config_for(ctx).to_dict() != PatchConfig.from_env().to_dict()):
            raise ValueError('Work directory context does not match this repository/tag')
        if args.phase == 'tools':
            install_tools(root)
        elif args.phase.startswith('extract-'):
            extract_version(root, ctx, args.phase.split('-', 1)[1])
        elif args.phase == 'build':
            build(root, ctx)
        elif args.phase == 'pack':
            pack(root, ctx)
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

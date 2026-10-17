#!/usr/bin/env python3
"""Reproduce and publish the verified v0.11.1 -> v0.11.2 patch in Actions."""
import argparse, concurrent.futures, datetime, hashlib, json, os, pathlib, shutil
import subprocess, tarfile, urllib.request
from build_patch import scan, write

TAG = 'v0.11.2-v0.11.1.patch'
SEVEN_ZIP_URL = 'https://github.com/ip7z/7zip/releases/download/26.04/7z2604-linux-x64.tar.xz'
SEVEN_ZIP_SHA256 = 'fc0327ba27e89bd086cf426dff17d77de582953cdbbc10a6576540a06853ffcd'
LIMIT = 1_000_000_000

def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)

def gh_json(path):
    return json.loads(subprocess.check_output(['gh', 'api', path]))

def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def download(url, path, sha256, size=None):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=120) as source, path.open('wb') as target:
                shutil.copyfileobj(source, target, 8 * 1024 * 1024)
            if size is not None and path.stat().st_size != size:
                raise ValueError(f'Incorrect download size: {path.name}')
            if digest(path) != sha256:
                raise ValueError(f'SHA256 mismatch: {path.name}')
            print('SHA256 verified:', path.name, flush=True)
            return
        except Exception:
            if attempt == 2:
                raise

def checksum_files(out):
    (out / 'SHA256SUMS.txt').write_text(''.join(
        f'{digest(p)}  {p.name}\n' for p in sorted(out.iterdir())
        if p.is_file() and p.name != 'SHA256SUMS.txt'), encoding='utf-8')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', type=pathlib.Path, required=True)
    args = parser.parse_args()
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    if shutil.disk_usage(root).free < 38_000_000_000:
        raise RuntimeError('At least 38 GB free disk space is required')
    repo = os.environ['PATCH_REPOSITORY']
    releases = gh_json(f'repos/{repo}/releases')
    matches = [r for r in releases if r['tag_name'] == TAG]
    if len(matches) != 1 or not matches[0]['draft'] or matches[0]['assets']:
        raise RuntimeError('Exactly one empty release draft is required; refusing to overwrite assets or a published release')
    release = matches[0]
    pwsh = shutil.which('pwsh')
    if not pwsh:
        raise RuntimeError('PowerShell is required for actual patch validation')
    tools = root / 'tools'; tools.mkdir()
    archive = tools / '7zip.tar.xz'
    download(SEVEN_ZIP_URL, archive, SEVEN_ZIP_SHA256)
    with tarfile.open(archive) as tf:
        tf.extractall(tools, filter='data')
    seven = tools / '7zz'
    sources = []
    for version, target in [('0.11.1', 'old'), ('0.11.2', 'new')]:
        release = gh_json(f'repos/Kruk2/jasna/releases/tags/v{version}')
        assets = sorted((a for a in release['assets']
                         if a['name'].startswith(f'jasna-windows-{version}.7z.')), key=lambda a: a['name'])
        if [a['name'].rsplit('.', 1)[-1] for a in assets] != ['001', '002', '003', '004']:
            raise RuntimeError('Unexpected upstream Windows volume list')
        sources.append({'tag': release['tag_name'], 'url': release['html_url'],
                        'assets': [{k: a[k] for k in ['name', 'size', 'digest', 'browser_download_url']}
                                   for a in assets]})
        downloads = root / f'download-{target}'; downloads.mkdir()
        def get_asset(a):
            if not a.get('digest', '').startswith('sha256:'):
                raise RuntimeError('Upstream SHA256 is required')
            download(a['browser_download_url'], downloads / a['name'],
                     a['digest'].split(':', 1)[1], a['size'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(get_asset, assets))
        run([seven, 'x', downloads / assets[0]['name'], f'-o{root / target}', '-y', '-bsp0', '-mmt=2'])
        shutil.rmtree(downloads)
    stage = root / 'stage'
    run(['python3', pathlib.Path(__file__).with_name('build_patch.py'), root / 'old', root / 'new', stage])
    write(stage / 'source-assets.json', sources)
    out = root / 'artifacts'; out.mkdir()
    packed = out / f'{TAG}.7z'
    run([seven, 'a', '-t7z', '-mx=5', '-md=64m', '-mmt=2', '-bsp0', packed, '.'], cwd=stage)
    if packed.stat().st_size > LIMIT:
        with packed.open('rb') as source:
            part = 1
            while chunk := source.read(8 * 1024 * 1024):
                with pathlib.Path(f'{packed}.{part:03d}').open('wb') as target:
                    remaining = LIMIT
                    while chunk:
                        target.write(chunk); remaining -= len(chunk)
                        if remaining == 0:
                            break
                        chunk = source.read(min(remaining, 8 * 1024 * 1024))
                part += 1
        packed.unlink()
    archives = sorted(out.glob('*.7z*'))
    unpacked = root / 'verified-patch'
    run([seven, 'x', archives[0], f'-o{unpacked}', '-y', '-bsp0'])
    run([pwsh, '-NoLogo', '-NoProfile', '-File', unpacked / 'Apply-Patch.ps1', '-InstallDir', root / 'old'])
    expected = json.loads((unpacked / 'new-manifest.json').read_text())
    if scan(root / 'old') != expected or scan(root / 'new') != expected:
        raise RuntimeError('Updated file set and SHA256 must exactly match upstream v0.11.2')
    for name in ['diff.json', 'source-assets.json']:
        shutil.copyfile(stage / name, out / name)
    write(out / 'verification.json', {
        'validated_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'source_archive_parts_sha256_verified': 8,
        'source_archives_extraction_crc_verified': True,
        'patch_archive_extraction_crc_verified': True,
        'actual_powershell_apply_exit_code': 0,
        'updated_file_set_and_sha256_exactly_match_target': True,
        'target_files': len(expected), 'target_bytes': sum(x['size'] for x in expected.values()),
        'validation_platform': 'Linux x64; Windows application/GPU not launched',
        'actions_run_url': f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"})
    checksum_files(out)
    release = gh_json(f'repos/{repo}/releases/{release["id"]}')
    if not release['draft']:
        raise RuntimeError('Refusing to overwrite an already published release')
    if release['assets']:
        raise RuntimeError('Draft already has assets; inspect them before retrying')
    files = sorted(out.iterdir())
    run(['gh', 'release', 'upload', TAG, *files, '--repo', repo])
    remote = gh_json(f'repos/{repo}/releases/{release["id"]}')
    received = {a['name']: a for a in remote['assets']}
    if set(received) != {p.name for p in files}:
        raise RuntimeError('Remote asset list mismatch')
    for p in files:
        a = received[p.name]
        if a['size'] != p.stat().st_size or a.get('digest') != f'sha256:{digest(p)}':
            raise RuntimeError(f'Remote asset SHA256 mismatch or unavailable: {p.name}')
    diff = json.loads((stage / 'diff.json').read_text())
    sizes = ', '.join(f'{p.name}: {p.stat().st_size:,} bytes' for p in archives)
    notes = root / 'release-notes.md'
    notes.write_text(f'''Jasna Windows **v0.11.1 → v0.11.2** 文件级差异补丁，非上游官方发布。

新增 {len(diff['added'])} 个、修改 {len(diff['modified'])} 个、删除 {len(diff['deleted'])} 个文件。

压缩附件：{sizes}。超过 1,000,000,000 字节时分卷。

关闭 Jasna 并备份旧版安装目录。将补丁解压到独立目录，在 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\\Apply-Patch.ps1 -InstallDir "C:\\Jasna"
```

安装目录应直接包含 jasna.exe；分卷须全部下载并从 .001 解压。脚本先校验全量旧版文件，再覆盖新增/修改文件、删除指定旧文件，最后校验全量新版文件。用户额外文件保留；旧版官方文件被修改时拒绝更新。失败时恢复备份，没有自动回滚。

已从最终压缩包实际执行 PowerShell 更新，更新结果与新版原始解压目录的 {len(expected):,} 个文件集合、大小和 SHA-256 完全一致。上游八个分卷和远端发布附件均经过 SHA-256 校验。未启动 Windows 应用或测试 GPU 功能。

详见 verification.json、diff.json、source-assets.json 和 SHA256SUMS.txt；原组件许可证见包内 upstream-notices/。

上游：[v0.11.1](https://github.com/Kruk2/jasna/releases/tag/v0.11.1)、[v0.11.2](https://github.com/Kruk2/jasna/releases/tag/v0.11.2)。
''', encoding='utf-8')
    run(['gh', 'release', 'edit', TAG, '--repo', repo, '--notes-file', notes, '--draft=false'])
    published = gh_json(f'repos/{repo}/releases/{release["id"]}')
    if published['draft']:
        raise RuntimeError('Release is still a draft')
    print('Published:', published['html_url'], flush=True)

if __name__ == '__main__':
    main()

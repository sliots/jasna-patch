#!/usr/bin/env python3
"""Build a file replacement patch from two extracted application directories."""
import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import shutil
import urllib.parse

from patch_config import validate_relative, validate_tag


def scan(root):
    paths = sorted(root.rglob('*'))
    if root.is_symlink() or any(p.is_symlink() for p in paths):
        raise ValueError('Symlinks are not supported in replacement patches')
    def one(path):
        with path.open('rb') as source:
            sha = hashlib.file_digest(source, 'sha256').hexdigest()
        return path.relative_to(root).as_posix(), {'size': path.stat().st_size, 'sha256': sha}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
        return dict(workers.map(one, (p for p in paths if p.is_file())))


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    for name in ('old', 'new', 'output'):
        parser.add_argument(name, type=pathlib.Path)
    parser.add_argument('--old-manifest', type=pathlib.Path)
    parser.add_argument('--old-tag', default='old')
    parser.add_argument('--new-tag', default='new')
    parser.add_argument('--source-repository', default='')
    parser.add_argument('--entry-file', default='')
    args = parser.parse_args()
    for tag in (args.old_tag, args.new_tag):
        validate_tag(tag)
    validate_relative(args.entry_file)
    if args.old_tag == args.new_tag:
        raise ValueError('Old and new labels must differ')
    if args.entry_file and any(not (root / args.entry_file).is_file() for root in (args.old, args.new)):
        raise ValueError(f'Both application roots must contain {args.entry_file}')
    old = json.loads(args.old_manifest.read_text()) if args.old_manifest else scan(args.old)
    new = scan(args.new)
    if not old or not new:
        raise ValueError('Both application directories must contain files')
    for label, manifest in [('old', old), ('new', new)]:
        if len({p.casefold() for p in manifest}) != len(manifest):
            raise ValueError(f'{label}: case-insensitive path collision')
    added = sorted(new.keys() - old.keys())
    deleted = sorted(old.keys() - new.keys())
    changed = sorted(p for p in old.keys() & new.keys() if old[p] != new[p])
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    for name in added + changed:
        target = out / 'payload' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.new / name, target)
    write(out / 'old-manifest.json', old)
    write(out / 'new-manifest.json', new)
    for name in ['LICENSE', 'LICENSE.txt', 'LICENSE.md', 'NOTICE', 'COPYING', 'LICENSING.md',
                 'RELEASE_SOURCES.md', 'licenses', 'assets/THIRD_PARTY_LICENSES.md', 'assets/THIRD_PARTY_MODELS.md']:
        source = args.new / name
        if source.exists():
            target = out / 'upstream-notices' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copyfile(source, target)
    diff = {
        'format_version': 2, 'from': args.old_tag, 'to': args.new_tag,
        'source_repository': args.source_repository, 'entry_file': args.entry_file,
        'added': added, 'modified': changed, 'deleted': deleted,
        'payload_bytes': sum(new[p]['size'] for p in added + changed),
        'old_files': len(old), 'new_files': len(new),
    }
    write(out / 'diff.json', diff)
    shutil.copyfile(pathlib.Path(__file__).with_name('Apply-Patch.ps1'), out / 'Apply-Patch.ps1')
    sources = ''
    if args.source_repository:
        sources = '\nSources:\n' + ''.join(
            f'https://github.com/{args.source_repository}/releases/tag/{urllib.parse.quote(tag, safe="")}\n'
            for tag in (args.old_tag, args.new_tag))
    marker = f'The installation root must contain {args.entry_file}.\n' if args.entry_file else ''
    (out / 'README.txt').write_text(
        f'File replacement patch: {args.old_tag} -> {args.new_tag}\n\n'
        'Close the application and BACK UP the installation before updating.\n'
        'Extract the complete patch into a separate folder. Download all parts if split.\n'
        'Run in PowerShell:\n'
        '  powershell -NoProfile -ExecutionPolicy Bypass -File .\\Apply-Patch.ps1 -InstallDir "C:\\Application"\n'
        + marker + f'All baseline files ({args.old_tag}) are verified before changes.\n'
        'Added/modified files are copied; explicitly obsolete files are deleted.\n'
        f'All target files ({args.new_tag}) are verified after updating.\n'
        'Unlisted user files are retained. Modified baselines are rejected.\n'
        'There is no automatic rollback; restore your backup on failure.\n'
        'Original component licenses and notices continue to apply.\n' + sources, encoding='utf-8')
    print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in diff.items()}, indent=2), flush=True)


if __name__ == '__main__':
    main()

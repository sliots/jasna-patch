#!/usr/bin/env python3
"""Build a file replacement patch from extracted official Windows releases."""
import argparse,concurrent.futures,hashlib,json,pathlib,shutil

def scan(root):
 paths=sorted(p for p in root.rglob('*') if p.is_file())
 def one(p):
  if p.is_symlink():raise ValueError(f'Symlink not supported: {p}')
  with p.open('rb') as f:d=hashlib.file_digest(f,'sha256').hexdigest()
  return p.relative_to(root).as_posix(),{'size':p.stat().st_size,'sha256':d}
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:return dict(ex.map(one,paths))

def write(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def main():
 a=argparse.ArgumentParser();a.add_argument('old',type=pathlib.Path);a.add_argument('new',type=pathlib.Path);a.add_argument('output',type=pathlib.Path);a.add_argument('--old-manifest',type=pathlib.Path);args=a.parse_args()
 out=args.output;out.mkdir(parents=True,exist_ok=False)
 old=json.loads(args.old_manifest.read_text()) if args.old_manifest else scan(args.old)
 new=scan(args.new)
 for label,manifest in [('old',old),('new',new)]:
  if len({p.casefold() for p in manifest})!=len(manifest):raise ValueError(f'{label}: case-insensitive path collision')
 added=sorted(new.keys()-old.keys());deleted=sorted(old.keys()-new.keys());changed=sorted(p for p in old.keys() & new.keys() if old[p]!=new[p])
 for p in added+changed:
  dst=out/'payload'/p;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(args.new/p,dst)
 write(out/'old-manifest.json',old);write(out/'new-manifest.json',new)
 for name in ['LICENSE','NOTICE','LICENSING.md','RELEASE_SOURCES.md','licenses','assets/THIRD_PARTY_LICENSES.md','assets/THIRD_PARTY_MODELS.md']:
  source=args.new/name
  if source.exists():
   target=out/'upstream-notices'/name;target.parent.mkdir(parents=True,exist_ok=True)
   if source.is_dir():shutil.copytree(source,target)
   else:shutil.copyfile(source,target)
 diff={'from':'v0.11.1','to':'v0.11.2','added':added,'modified':changed,'deleted':deleted,'payload_bytes':sum(new[p]['size'] for p in added+changed),'old_files':len(old),'new_files':len(new)}
 write(out/'diff.json',diff)
 shutil.copyfile(pathlib.Path(__file__).with_name('Apply-Patch.ps1'),out/'Apply-Patch.ps1')
 (out/'README.txt').write_text('Jasna Windows v0.11.1 -> v0.11.2 file replacement patch\n\nClose Jasna and BACK UP the installation before updating.\nExtract this entire patch (all volumes together if split) into a separate folder.\nRun in PowerShell:\n  powershell -NoProfile -ExecutionPolicy Bypass -File .\\Apply-Patch.ps1 -InstallDir "C:\\Jasna"\nInstallDir must be the folder directly containing jasna.exe.\nThe script verifies every official v0.11.1 file before changing anything,\nthen installs added/modified files, removes files listed in diff.json,\nand verifies every official v0.11.2 file. Unlisted user files are retained.\nA modified installation is rejected. There is no automatic rollback; restore your backup on failure.\n\nSources: https://github.com/Kruk2/jasna/releases/tag/v0.11.1\n         https://github.com/Kruk2/jasna/releases/tag/v0.11.2\nNot an official upstream release. Original component licenses and notices apply.\n',encoding='utf-8')
 print(json.dumps({k:v if not isinstance(v,list) else len(v) for k,v in diff.items()},indent=2),flush=True)

if __name__=='__main__':main()

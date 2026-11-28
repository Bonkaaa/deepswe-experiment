from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path
from ..results.storage import scratch_directory

EXCLUDED={'.git','__pycache__','.pytest_cache'}
def copy_snapshot(source,dest):
    source=Path(source); source_root=source.resolve(); dest=Path(dest); dest.mkdir(parents=True,exist_ok=True)
    for p in source.rglob('*'):
        rel=p.relative_to(source)
        if any(x in EXCLUDED for x in rel.parts): continue
        if p.is_symlink():
            linkname=os.readlink(p)
            if linkname.startswith('/') or Path(linkname).is_absolute() or not (p.parent/linkname).resolve().is_relative_to(source_root):
                raise ValueError(f'Unsafe symlink in snapshot: {rel.as_posix()} -> {linkname}')
            target=dest/rel; target.parent.mkdir(parents=True,exist_ok=True); os.symlink(linkname,target)
        elif p.is_file():
            target=dest/rel; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,target)

def collect_diff(baseline,final,output_dir,allowed_prefixes,language=None):
    from ..tasks.patcheval_tests import test_exclude_pathspecs
    output_dir=Path(output_dir).resolve(); output_dir.mkdir(parents=True,exist_ok=True)
    baseline=Path(baseline).resolve(); final=Path(final).resolve()
    # Put temporary Git objects under the selected run artifact, avoiding writes
    # to an unrelated host temp directory and keeping scratch data scoped.
    with scratch_directory(output_dir) as tmp:
        tmp=tmp.resolve()
        repo=tmp/'worktree'; repo.mkdir()
        gitdir=tmp/'gitdata'
        git_env=os.environ.copy()
        for key in tuple(git_env):
            if key=='GIT_CONFIG_COUNT' or key.startswith(('GIT_CONFIG_KEY_','GIT_CONFIG_VALUE_')):
                git_env.pop(key)
        init=subprocess.run(['git','-c','core.longpaths=true','init','--bare','-q',str(gitdir)],
                            cwd=tmp,check=False,capture_output=True,text=True,env=git_env)
        if init.returncode:
            raise RuntimeError(f'Temporary Git initialization failed: {init.stderr[-1000:]}')
        def git(*args):
            return subprocess.run(['git','-c','core.longpaths=true','--git-dir',str(gitdir),'--work-tree',str(repo),
                                   '-c','core.autocrlf=false',*args],cwd=repo,check=True,capture_output=True,env=git_env).stdout
        copy_snapshot(baseline,repo); git('add','-f','.')
        git('-c','user.name=Harness','-c','user.email=harness@local','commit','-qm','baseline','--allow-empty')
        for p in list(repo.iterdir()):
            if p.is_dir(): shutil.rmtree(p)
            else: p.unlink()
        copy_snapshot(final,repo)
        git('rm','-rfq','--cached','--ignore-unmatch','.')
        git('add','-f','-A')
        full=git('diff','--cached','--binary','--no-renames','HEAD')
        pathspecs=[*allowed_prefixes]
        if language:
            pathspecs.extend(test_exclude_pathspecs(language))
        elif pathspecs and not any(str(p).endswith('_test.go') for p in pathspecs):
            pathspecs.append(':!*_test.go')
        product=git('diff','--cached','--binary','--no-renames','HEAD','--',*pathspecs)
    a=output_dir/'full_worktree.diff'; b=output_dir/'agent_patch.diff'
    a.write_bytes(full); b.write_bytes(product)
    return a,b

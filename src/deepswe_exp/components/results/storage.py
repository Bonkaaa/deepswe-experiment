from __future__ import annotations
import hashlib
import json
import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def read_json(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def write_json(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8',newline='\n') as f:
        json.dump(value,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
    tmp.replace(path)
def append_event(path,event):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf-8') as f:
        f.write(json.dumps(event,ensure_ascii=False,default=str)+'\n'); f.flush(); os.fsync(f.fileno())

@contextmanager
def scratch_directory(parent,prefix='.scratch-'):
    """Create and remove an ordinary workspace-local scratch folder.

    `tempfile.TemporaryDirectory` applies a restrictive Windows ACL on some
    managed hosts; ordinary directories inherit the workspace ACL instead.
    """
    parent=Path(parent); parent.mkdir(parents=True,exist_ok=True)
    path=parent/(prefix+uuid.uuid4().hex)
    path.mkdir()
    try: yield path
    finally:
        def make_writable_and_retry(function, target, error):
            os.chmod(target, stat.S_IWRITE)
            function(target)
        shutil.rmtree(path,onerror=make_writable_and_retry)
def utc_now(): return datetime.now(timezone.utc).isoformat()
@contextmanager
def study_lock(root):
    root=Path(root); root.mkdir(parents=True,exist_ok=True); path=root/'.runner.lock'
    try: fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError: raise RuntimeError(f'Study locked: {path}; check PID before removing stale lock')
    try:
        os.write(fd,str(os.getpid()).encode()); os.close(fd); yield
    finally: path.unlink(missing_ok=True)
def run_dir(settings,run):
    return settings.output_root/run['task_id']/run['branch']/str(run['seed'])/run['attempt_id']
def protocol(settings,prepared,task_ids=None):
    selected=list(task_ids or prepared)
    validations={task:read_json(settings.output_root/task/'validation/validation.json') for task in selected}
    return digest({'schema_version':1,'config':settings.config,'model':settings.model,
                   'prompt':settings.prompt_path.read_text(encoding='utf-8'),
                   'prepared':{task:prepared[task] for task in selected},'validation':validations})

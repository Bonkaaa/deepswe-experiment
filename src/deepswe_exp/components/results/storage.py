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
def fetch_ollama_metadata(base_url: str, model_tag: str, timeout: float = 2.0) -> dict:
    import urllib.request
    meta = {"version": None, "digest": None, "quantization": None, "details": {}}
    base = base_url.rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/api/version", headers={"User-Agent": "deepswe"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            meta["version"] = json.loads(resp.read().decode("utf-8")).get("version")
    except Exception:
        pass
    try:
        data = json.dumps({"name": model_tag}).encode("utf-8")
        req = urllib.request.Request(f"{base}/api/show", data=data, headers={"Content-Type": "application/json", "User-Agent": "deepswe"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            show = json.loads(resp.read().decode("utf-8"))
            meta["details"] = show.get("details", {})
            meta["quantization"] = meta["details"].get("quantization_level")
            meta["model_info"] = show.get("model_info", {})
    except Exception:
        pass
    try:
        req = urllib.request.Request(f"{base}/api/tags", headers={"User-Agent": "deepswe"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tags = json.loads(resp.read().decode("utf-8")).get("models", [])
            for m in tags:
                if m.get("name") == model_tag or m.get("model") == model_tag:
                    meta["digest"] = m.get("digest")
                    if not meta["quantization"]:
                        meta["quantization"] = m.get("details", {}).get("quantization_level")
                    break
    except Exception:
        pass
    return meta


def protocol(settings, prepared, task_ids=None, model_metadata=None):
    selected = list(task_ids or prepared)
    validations = {task: read_json(settings.output_root / task / 'validation/validation.json') for task in selected}
    payload = {
        'schema_version': 1,
        'config': settings.config,
        'model': settings.model,
        'prompt': settings.prompt_path.read_text(encoding='utf-8'),
        'prepared': {task: prepared[task] for task in selected},
        'validation': validations,
    }
    if getattr(settings, "profile_id", None) is not None or getattr(settings, "model_provider", "google") != "google":
        payload['model_provider'] = getattr(settings, "model_provider", "google")
        payload['profile_id'] = getattr(settings, "profile_id", None)
    if getattr(settings, "model_provider", "google") == "ollama":
        payload["ollama"] = {
            "base_url": settings.ollama_base_url,
            "num_ctx": settings.ollama_num_ctx,
            "options": getattr(settings, "ollama_options", {}),
            "metadata": model_metadata or {},
        }
    return digest(payload)

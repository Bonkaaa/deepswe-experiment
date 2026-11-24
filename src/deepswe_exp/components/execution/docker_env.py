from __future__ import annotations
import io
import json
import shlex
import tarfile
import threading
from pathlib import Path
import docker

BENCHMARK_CONTAINER_POLICY={
    'network_mode':'none',
    'cap_drop':['ALL'],
    'cap_add':['CHOWN','DAC_OVERRIDE','FOWNER','SETUID','SETGID'],
    'security_opt':['no-new-privileges'],
}


def benchmark_container_limits(metadata):
    limits=(metadata or {}).get('environment',{})
    return {'cpus':limits.get('cpus',2),'memory_mb':limits.get('memory_mb',8192)}

class DockerFailure(RuntimeError): pass

def docker_status():
    client = None
    try:
        client = docker.from_env()
        info = client.info()
        return 'available' if info.get('OSType') == 'linux' else 'Linux engine required'
    except Exception as exc:
        return f'unavailable ({type(exc).__name__})'
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

def safe_extract(data,destination):
    destination=Path(destination); destination.mkdir(parents=True,exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for m in archive.getmembers():
            target=(destination/m.name).resolve()
            if not target.is_relative_to(destination.resolve()) or m.issym() or m.islnk() or m.isdev():
                raise DockerFailure('Unsafe archive member')
        archive.extractall(destination,filter='data')

class Sandbox:
    def __init__(self,client,image,task=None):
        metadata=getattr(task,'metadata',task) or {}
        limits=metadata.get('environment',{})
        self.cwd=getattr(task,'workdir','/app')
        self.container=client.containers.run(image,['sleep','infinity'],detach=True,
            network_disabled=True,working_dir=self.cwd,user='root',
            environment={'PAGER':'cat','MANPAGER':'cat','LESS':'-R','PIP_PROGRESS_BAR':'off','TQDM_DISABLE':'1'},
            nano_cpus=int(limits.get('cpus',2)*1e9),mem_limit=f"{limits.get('memory_mb',8192)}m",
            cap_drop=['ALL'],cap_add=['CHOWN','DAC_OVERRIDE','FOWNER','SETUID','SETGID'],
            security_opt=['no-new-privileges'],init=True)
        self.user='root'
    def execute(self,command,timeout=120):
        token=__import__('uuid').uuid4().hex
        # timeout kills the entire command process group, including grandchildren.
        wrapper=f"timeout --signal=TERM --kill-after=0.2s {max(.01,timeout):.3f}s bash -c {shlex.quote(command)}"
        result={}
        def worker():
            try:
                options={'user':self.user,'workdir':self.cwd}
                if self.user!='root': options['environment']={'HOME':'/home/experiment'}
                result['value']=self.container.exec_run(['bash','-c',wrapper],**options)
            except Exception as e: result['error']=e
        thread=threading.Thread(target=worker,daemon=True); thread.start(); thread.join(timeout+10)
        if thread.is_alive():
            self.container.kill()
            raise DockerFailure('Command timeout: container killed')
        if 'error' in result: raise DockerFailure(type(result['error']).__name__)
        rc,out=result['value']
        return rc,out.decode('utf-8',errors='replace')
    def checked(self,command,timeout=120):
        rc,out=self.execute(command,timeout)
        if rc: raise DockerFailure(f'Command failed (exit {rc}): {out[-3000:]}')
        return out
    def put_bytes(self,path,data):
        parent,name=path.rsplit('/',1)
        self.checked('mkdir -p '+shlex.quote(parent))
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as t:
            info=tarfile.TarInfo(name); info.size=len(data); info.mode=0o644; t.addfile(info,io.BytesIO(data))
        if not self.container.put_archive(parent,stream.getvalue()):
            raise DockerFailure('Docker rejected file archive')
    def put_tree(self,source,dest):
        source=Path(source); stream=io.BytesIO()
        self.checked('mkdir -p '+shlex.quote(dest))
        with tarfile.open(fileobj=stream,mode='w') as t:
            for p in sorted(source.rglob('*')):
                if p.is_symlink(): raise DockerFailure('Symlink snapshots are not supported')
                if p.is_file() and not any(x in ('.git','__pycache__','.pytest_cache') for x in p.relative_to(source).parts):
                    t.add(p,arcname=p.relative_to(source).as_posix(),recursive=False)
        if not self.container.put_archive(dest,stream.getvalue()):
            raise DockerFailure('Docker rejected snapshot archive')
    def get_tree(self,source,dest):
        stream,_=self.container.get_archive(source)
        safe_extract(b''.join(stream),dest)
    def close(self):
        self.container.remove(force=True)
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

class DockerBackend:
    def __init__(self): self.client=docker.from_env()
    def resolve(self,image):
        try: obj=self.client.images.get(image)
        except docker.errors.ImageNotFound: obj=self.client.images.pull(image)
        digests=obj.attrs.get('RepoDigests',[])
        return digests[0] if digests else obj.id
    def sandbox(self,image,task): return Sandbox(self.client,image,task)
    def verifier_image(self,task,image):
        if task.benchmark:
            return image
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as t:
            for p in sorted((task.path/'tests').iterdir()):
                if p.is_file():
                    data=p.read_bytes()
                    if p.name=='Dockerfile':
                        lines=data.decode().splitlines()
                        data=('\n'.join('FROM '+image if l.strip().startswith('FROM ') else l for l in lines)+'\n').encode()
                    elif p.name.endswith(('.sh','.py','.patch','.json','.toml','.md','.txt')):
                        data=data.replace(b'\r\n',b'\n')
                    info=tarfile.TarInfo(p.name); info.size=len(data)
                    info.mode=0o755 if p.name.endswith('.sh') else 0o644
                    t.addfile(info,io.BytesIO(data))
        stream.seek(0)
        built,_=self.client.images.build(fileobj=stream,custom_context=True,rm=True,timeout=1800)
        return built.id
    def prepare_swebench_image(self,task,image):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as archive:
            data=(f'FROM {image}\nENV GOMODCACHE=/opt/swebench-go-mod-cache\n'
                  f'RUN mkdir -p "$GOMODCACHE" && cd {task.workdir} && go mod download\n').encode()
            info=tarfile.TarInfo('Dockerfile'); info.size=len(data); info.mode=0o644
            archive.addfile(info,io.BytesIO(data))
        stream.seek(0)
        built,_=self.client.images.build(fileobj=stream,custom_context=True,rm=True,
                                         network_mode='default',timeout=1800)
        return built.id

def checkout_base(box,task):
    sha=task.base_commit
    if len(sha)!=40 or any(c not in '0123456789abcdef' for c in sha): raise ValueError('Invalid commit')
    cwd=shlex.quote(box.cwd)
    box.checked(f'git config --global --add safe.directory {cwd} && git -C {cwd} reset --hard {sha} && git -C {cwd} clean -fdx')

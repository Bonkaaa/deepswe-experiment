from __future__ import annotations
import json
import math
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from ..results.storage import append_event

class QuotaUnavailable(RuntimeError): pass
class InputBudget(RuntimeError): pass


class LocalLedger:
    """Event recorder for local models without cloud rate limits."""
    def __init__(self, path, model, project="local", clock=time.time):
        self.path = Path(path)
        self.model = model
        self.project = project
        self.clock = clock

    def remaining(self) -> int:
        return 999_999

    def reserve(self, estimate: int, deadline: float, monotonic=time.monotonic):
        append_event(self.path, {
            'type': 'request',
            'time': self.clock(),
            'model': self.model,
            'project': self.project,
            'tokens': estimate,
        })

    def usage(self, usage: dict | None):
        append_event(self.path, {
            'type': 'usage',
            'time': self.clock(),
            'model': self.model,
            'project': self.project,
            'usage': usage or {},
        })

class QuotaLedger:
    def __init__(self,path,quota,model,project='default',clock=time.time,sleep=time.sleep):
        self.path=path; self.quota=quota; self.model=model; self.project=project
        self.clock=clock; self.sleep=sleep
    def events(self):
        if not self.path.exists(): return []
        rows=[]
        for line in self.path.read_text(encoding='utf-8').splitlines():
            try: row=json.loads(line)
            except json.JSONDecodeError: raise QuotaUnavailable('Quota ledger damaged; inspect before continuing')
            if row.get('model')==self.model and (
                row.get('project')==self.project
                or (row.get('kind')=='connectivity_check' and row.get('project') in (None,'default'))
            ):
                rows.append(row)
        return rows
    @contextmanager
    def lock(self):
        """Serialize reserve/check operations across runner processes on Windows and Linux."""
        self.path.parent.mkdir(parents=True,exist_ok=True)
        lock_path=self.path.with_suffix(self.path.suffix+'.lock')
        with lock_path.open('a+b') as stream:
            if stream.tell()==0:
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            if __import__('os').name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            try: yield
            finally:
                if __import__('os').name=='nt':
                    import msvcrt
                    stream.seek(0); msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
    def day(self,t):
        return datetime.fromtimestamp(t,timezone.utc).astimezone(ZoneInfo('America/Los_Angeles')).date()
    def remaining(self):
        with self.lock():
            now=self.clock()
            return self.quota['rpd']-sum(e['type']=='request' and self.day(e['time'])==self.day(now) for e in self.events())
    def reserve(self,estimate,deadline,monotonic=time.monotonic):
        cap=math.floor(.8*self.quota['tpm'])
        if estimate>cap: raise InputBudget('Input exceeds conservative TPM budget')
        while True:
            with self.lock():
                now=self.clock()
                events=self.events()
                daily=sum(e['type']=='request' and self.day(e['time'])==self.day(now) for e in events)
                if daily>=self.quota['rpd']: raise QuotaUnavailable('Daily quota exhausted')
                recent=[e for e in events if e['type']=='request' and now-e['time']<60]
                if len(recent)<max(1,math.floor(.8*self.quota['rpm'])) and sum(e['tokens'] for e in recent)+estimate<=cap:
                    event={'type':'request','time':now,'model':self.model,'project':self.project,'tokens':estimate}
                    append_event(self.path,event); return
                delay=max(.05,60-(now-min((e['time'] for e in recent),default=now))+.1)
            if monotonic()+delay>=deadline: raise TimeoutError('Time budget while waiting for quota')
            self.sleep(delay)
    def usage(self,usage):
        append_event(self.path,{'type':'usage','time':self.clock(),'model':self.model,'project':self.project,'usage':usage})

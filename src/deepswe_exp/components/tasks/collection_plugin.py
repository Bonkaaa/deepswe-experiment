"""Trusted collection plugin; injected into /tmp, never supplied to repair agent."""
import inspect
import json
import os
import re
from pathlib import Path

try:
    import pytest
except ImportError:
    pytest = None

def _is_in_model(nodeid):
    return nodeid and ('tests/unit/morphing/model/' in nodeid or nodeid.startswith('tests/unit/morphing/model/'))

if pytest is not None:
    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_make_parametrize_id(config, val, argname):
        outcome = yield
        res = outcome.get_result()

        frame = inspect.currentframe().f_back
        nodeid = None
        while frame:
            self_obj = frame.f_locals.get('self')
            nodeid = getattr(self_obj, 'nodeid', None)
            if nodeid:
                break
            frame = frame.f_back

        if not _is_in_model(nodeid):
            if res == str(val):
                name = getattr(val, '__name__', None) or getattr(val, '_name', None)
                if isinstance(name, str):
                    outcome.force_result(name)
                    return
                outcome.force_result(None)
                return

        if res and (' at 0x' in res or ' object at 0x' in res):
            clean = re.sub(r' at 0x[0-9a-fA-F]+', '', res)
            outcome.force_result(clean)

def pytest_collection_finish(session):
    rows=[]
    for item in session.items:
        func=getattr(item,'obj',None)
        original=getattr(item,'originalname',None) or item.name.split('[')[0]
        path=Path(item.path).resolve()
        source=inspect.getsourcefile(func) if func is not None else None
        if source and Path(source).resolve()!=path:
            raise RuntimeError('Inherited/dynamic tests cannot be ablated safely: '+item.nodeid)
        rows.append({'node_id':item.nodeid,'file':path.relative_to(Path('/app')).as_posix(),
                     'function':original,'class':item.cls.__name__ if getattr(item,'cls',None) else None,
                     'fixtures':sorted(getattr(item,'fixturenames',())),
                     'description':inspect.getdoc(func) if func is not None else None})
    Path(os.environ.get('COLLECTION_OUT','/tmp/collection.json')).write_text(json.dumps(rows))

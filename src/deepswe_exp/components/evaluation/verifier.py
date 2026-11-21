from __future__ import annotations
import json
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path
from ..results.storage import write_json,read_json
from ..results.schemas import EvaluationResult
from ..execution.docker_env import checkout_base,DockerFailure

PLUGIN=Path(__file__).parents[1]/'tasks/collection_plugin.py'

class PatchApplicationFailure(RuntimeError): pass

def parse_result(folder,rc,timed_out=False)->EvaluationResult:
    folder=Path(folder)
    if timed_out: return {'status':'timeout','reward':None}
    path=folder/'reward.json'; report=folder/'ctrf.json'
    if not path.exists(): return {'status':'infrastructure_error','reward':None,'detail':'Missing reward.json','exit_code':rc}
    try: reward=read_json(path)
    except (ValueError,OSError): return {'status':'infrastructure_error','reward':None,'detail':'Invalid reward.json'}
    if reward.get('apply_failed'): return {'status':'apply_failure','reward':reward.get('reward'),'benchmark':reward}
    if not report.exists() or rc!=0: return {'status':'infrastructure_error','reward':None,'benchmark':reward,'detail':'Missing report or verifier process failed'}
    try:
        report_data=read_json(report)
        rows=report_data.get('results',{}).get('tests',[])
    except (ValueError,OSError,AttributeError,TypeError):
        return {'status':'infrastructure_error','reward':None,'benchmark':reward,'detail':'Invalid ctrf.json'}
    if not isinstance(rows,list) or not rows:
        return {'status':'infrastructure_error','reward':None,'benchmark':reward,'detail':'Empty or invalid test report'}
    if reward.get('reward') not in (0,1): return {'status':'infrastructure_error','reward':None,'benchmark':reward}
    return {'status':'pass' if reward['reward']==1 else 'fail_test','reward':reward['reward'],'benchmark':reward}

def evaluate_patch(backend,task,image,patch,output,protocol_hash=''):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    timeout=task.metadata.get('verifier',{}).get('timeout_sec',1800)
    rc=-1; stdout=''; timed_out=False
    try:
        with backend.sandbox(image,task) as box:
            checkout_base(box,task)
            box.put_bytes('/logs/artifacts/model.patch',Path(patch).read_bytes().replace(b'\r\n',b'\n'))
            rc,stdout=box.execute('bash /tests/test.sh',timeout)
            timed_out=rc in (124,137)
            (output/'test-stdout.txt').write_text(stdout,encoding='utf-8')
            try:
                report_root=output/'verifier'
                box.get_tree('/logs/verifier',report_root)
                # Engine versions differ on whether get_archive(directory)
                # includes the directory's own name as the first tar component.
                nested=report_root/'verifier'
                if nested.is_dir():
                    for child in nested.iterdir():
                        target=report_root/child.name
                        if not target.exists(): child.replace(target)
                    nested.rmdir()
            except Exception: pass  # Missing artifacts become explicit infrastructure errors below.
    except Exception as exc:
        (output/'test-stdout.txt').write_text(stdout,encoding='utf-8')
        result={'status':'infrastructure_error','reward':None,
                'detail':f'{type(exc).__name__}: {str(exc)[:1000]}'}
        result.update(schema_version=1,protocol_hash=protocol_hash)
        write_json(output/'result.json',result)
        return result
    result=parse_result(output/'verifier',rc,timed_out)
    result.update(schema_version=1,protocol_hash=protocol_hash)
    write_json(output/'result.json',result)
    return result

def evaluate_supplied_tests(backend,task,image,snapshot,manifest,patch,output,timeout=1800,protocol_hash=''):
    """Run assigned test IDs from the trusted filtered snapshot, never agent-edited tests."""
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    node_ids=manifest.get('kept_node_ids',[])
    if not node_ids:
        result={'schema_version':1,'protocol_hash':protocol_hash,'status':'not_applicable','passed':0,'failed':0,'total':0}
        write_json(output/'result.json',result); return result
    rc=-1; stdout=''; timed_out=False
    try:
        with backend.sandbox(image,task) as box:
            checkout_base(box,task)
            tests=Path(snapshot)/'tests'
            if not tests.is_dir(): raise RuntimeError('Filtered snapshot is missing tests/')
            box.put_tree(tests,'/app/tests')
            patch_data=Path(patch).read_bytes().replace(b'\r\n',b'\n')
            if patch_data:
                box.put_bytes('/tmp/model.patch',patch_data)
                try: box.checked('git apply --check /tmp/model.patch && git apply /tmp/model.patch')
                except Exception as exc: raise PatchApplicationFailure(str(exc)) from exc
            box.put_bytes('/tmp/experiment_collection.py',PLUGIN.read_bytes())
            box.put_bytes('/tmp/visible-nodeids.json',json.dumps(node_ids).encode())
            script=("import json,subprocess,sys,os\n"
                    "nodes=json.load(open('/tmp/visible-nodeids.json'))\n"
                    "env=dict(os.environ, PYTHONPATH='/tmp')\n"
                    "raise SystemExit(subprocess.run([sys.executable,'-m','pytest','-q','--disable-warnings',"
                    "'-p','experiment_collection','--junitxml=/tmp/visible-tests.xml',*nodes],env=env).returncode)\n")
            rc,stdout=box.execute('python -c '+shlex.quote(script),timeout)
            timed_out=rc in (124,137)
            report_path=output/'visible-tests.xml'
            try:
                box.get_tree('/tmp/visible-tests.xml',output)
                root=ET.parse(report_path).getroot()
                suites=[root] if root.tag=='testsuite' else list(root.iter('testsuite'))
                total=sum(int(s.get('tests','0')) for s in suites)
                failed=sum(int(s.get('failures','0'))+int(s.get('errors','0')) for s in suites)
                skipped=sum(int(s.get('skipped','0')) for s in suites)
            except Exception:
                total=failed=skipped=0
            (output/'stdout.txt').write_text(stdout,encoding='utf-8')
            if timed_out: status='timeout'
            elif rc!=0 and failed>0: status='fail_test'
            elif total!=len(node_ids): status='infrastructure_error'
            elif rc==0 and failed==0 and skipped==0: status='pass'
            elif rc==0 and failed==0 and skipped>0: status='partial'
            else: status='infrastructure_error'
            result={'schema_version':1,'protocol_hash':protocol_hash,'status':status,'passed':total-failed-skipped,'failed':failed,'skipped':skipped,
                    'total':total,'exit_code':rc}
    except Exception as exc:
        (output/'stdout.txt').write_text(stdout,encoding='utf-8')
        result={'schema_version':1,'protocol_hash':protocol_hash,
                'status':'apply_failure' if isinstance(exc,PatchApplicationFailure) else 'infrastructure_error',
                'passed':0,'failed':0,'total':len(node_ids),
                'detail':f'{type(exc).__name__}: {str(exc)[:1000]}'}
    write_json(output/'result.json',result)
    return result

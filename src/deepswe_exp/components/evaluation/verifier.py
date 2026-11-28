from __future__ import annotations
import json
import posixpath
import shlex
import re
import threading
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from ..results.storage import write_json,read_json
from ..results.schemas import EvaluationResult
from ..execution.docker_env import (BENCHMARK_CONTAINER_POLICY,benchmark_container_limits,
                                    checkout_base,DockerFailure)
from ..tasks.go_tests import parse_go_test_results,run_selected_go_tests

PLUGIN=Path(__file__).parents[1]/'tasks/collection_plugin.py'
_SWEBENCH_HARNESS_LOCK=threading.Lock()

class PatchApplicationFailure(RuntimeError): pass


class _RestrictedContainerCollection:
    def __init__(self,containers,limits):
        self._containers=containers
        self._limits=limits

    def __getattr__(self,name):
        return getattr(self._containers,name)

    def create(self,*args,**kwargs):
        cpus=self._limits.get('cpus',2)
        memory_mb=self._limits.get('memory_mb',8192)
        kwargs.update(
            network_disabled=True,
            **BENCHMARK_CONTAINER_POLICY,
            nano_cpus=int(cpus*1e9),
            mem_limit=f'{memory_mb}m',
        )
        return self._containers.create(*args,**kwargs)


class _RestrictedDockerClient:
    def __init__(self,client,limits):
        self._client=client
        self.containers=_RestrictedContainerCollection(client.containers,limits)

    def __getattr__(self,name):
        return getattr(self._client,name)


def _restricted_docker_client(client,limits):
    return _RestrictedDockerClient(client,limits or {})


def _classify_harness_error(log_text,patch_rejected=False):
    if patch_rejected:
        return 'apply_failure'
    if 'Timeout error:' in log_text or 'timed out' in log_text.lower():
        return 'timeout'
    return 'infrastructure_error'


def _has_terminal_patch_rejection(log_dir):
    from swebench.harness.constants import APPLY_PATCH_FAIL

    event=re.compile(r'^[0-9].* - INFO - '+re.escape(APPLY_PATCH_FAIL)+r':?[ ]*$')
    path=Path(log_dir)/'run_instance.log'
    if not path.is_file():
        return False
    return any(event.search(line) for line in path.read_text(encoding='utf-8',errors='replace').splitlines())


def _has_terminal_patch_application(log_dir):
    from swebench.harness.constants import APPLY_PATCH_PASS

    event=re.compile(r'^[0-9].* - INFO - '+re.escape(APPLY_PATCH_PASS)+r'(?::(?: verified already applied)?)?[ ]*$')
    path=Path(log_dir)/'run_instance.log'
    if not path.is_file():
        return False
    return any(event.search(line) for line in path.read_text(encoding='utf-8',errors='replace').splitlines())


def _has_go_compile_failure(log_dir):
    if not _has_terminal_patch_application(log_dir):
        return False
    output=Path(log_dir)/'test_output.txt'
    if not output.is_file():
        return False
    text=output.read_text(encoding='utf-8',errors='replace')
    return bool(
        re.search(r'(?m)^# [^\r\n]+$',text)
        and re.search(r'(?m)^.+\.go:\d+:\d+: .+$',text)
        and re.search(r'(?m)^FAIL\s+[^\r\n]+ \[build failed\]$',text)
    )


def _has_go_candidate_startup_panic(log_dir):
    if not _has_terminal_patch_application(log_dir):
        return False
    output=Path(log_dir)/'test_output.txt'
    if not output.is_file():
        return False
    text=output.read_text(encoding='utf-8',errors='replace')
    return bool(
        re.search(r'(?m)^panic: .+$',text)
        and re.search(r'(?m)^\s+[^\r\n]+\.go:\d+ \+0x[0-9a-f]+$',text)
        and re.search(r'(?m)^FAIL\s+[^\r\n]+\s+\d+(?:\.\d+)?s$',text)
    )


def _has_go_candidate_setup_failure(log_dir,patch,repository_root='/testbed'):
    if not _has_terminal_patch_application(log_dir):
        return False
    output=Path(log_dir)/'test_output.txt'
    if not output.is_file():
        return False
    text=output.read_text(encoding='utf-8',errors='replace')
    if not re.search(r'(?m)^FAIL\s+[^\r\n]+ \[setup failed\]$',text):
        return False
    changed_sources=set()
    in_hunk=False
    for line in Path(patch).read_text(encoding='utf-8',errors='replace').splitlines():
        if line.startswith('diff --git '):
            in_hunk=False
            continue
        if line.startswith('@@'):
            in_hunk=True
            continue
        if not in_hunk and line.startswith('+++ b/'):
            changed=line[6:].split('\t',1)[0]
            if changed.endswith('.go'):
                relative=PurePosixPath(changed)
                if not relative.is_absolute() and '..' not in relative.parts:
                    changed_sources.add(relative.as_posix())
    if not changed_sources:
        return False
    diagnostics=re.findall(
        r'(?m)^(.+\.go):\d+:\d+: (?:no required module provides package|cannot find package|cannot load package|cannot find module providing package|module lookup disabled by -mod=vendor)',
        text,
    )
    root=PurePosixPath(posixpath.normpath(str(repository_root).replace('\\','/')))
    if not root.is_absolute():
        return False
    for diagnostic in diagnostics:
        source=PurePosixPath(diagnostic.replace('\\','/'))
        combined=source if source.is_absolute() else root/source
        normalized=PurePosixPath(posixpath.normpath(combined.as_posix()))
        try:
            relative=normalized.relative_to(root)
        except ValueError:
            continue
        if relative.as_posix() in changed_sources:
            return True
    return False


def _read_harness_logs(log_dir):
    return '\n'.join(path.read_text(encoding='utf-8',errors='replace')
                     for path in log_dir.rglob('*') if path.is_file() and path.suffix in ('.log','.txt')) if log_dir.is_dir() else ''


def parse_swebench_report(report,task_id):
    instance=report.get(task_id) if isinstance(report,dict) else None
    if not isinstance(instance,dict):
        return {'status':'infrastructure_error','reward':None,'detail':'SWE-bench returned no instance report'}
    if instance.get('infra_failure'):
        return {'status':'infrastructure_error','reward':None,'benchmark':instance}
    if not instance.get('patch_successfully_applied'):
        return {'status':'infrastructure_error','reward':None,'benchmark':instance,
                'detail':'SWE-bench could not parse test results or confirm patch application'}
    tests=instance.get('tests_status')
    if not isinstance(tests,dict):
        return {'status':'infrastructure_error','reward':None,'benchmark':instance,
                'detail':'SWE-bench report has no parsed test results'}
    counts={}
    for key,label in (('FAIL_TO_PASS','f2p'),('PASS_TO_PASS','p2p')):
        outcome=tests.get(key)
        if not isinstance(outcome,dict) or not isinstance(outcome.get('success'),list) or not isinstance(outcome.get('failure'),list):
            return {'status':'infrastructure_error','reward':None,'benchmark':instance,
                    'detail':f'SWE-bench report is missing {key} results'}
        counts[f'{label}_passed']=len(outcome['success'])
        counts[f'{label}_failed']=len(outcome['failure'])
        counts[f'{label}_total']=len(outcome['success'])+len(outcome['failure'])
        counts[f'{label}_success']=outcome['success']
        counts[f'{label}_failure']=outcome['failure']
    reward=int(bool(instance.get('resolved')))
    return {'status':'pass' if reward else 'fail_test','reward':reward,'benchmark':counts}


def _run_official_harness(*,task,image,patch,client,run_id,output_dir,dataset_revision,harness_revision,timeout):
    from swebench.harness import run_evaluation
    from swebench.harness.run_evaluation import run_instance
    from swebench.harness.utils import make_test_spec

    record={**task.metadata,'image':image,
            'eval_script':task.metadata['eval_script'].replace('\r\n','\n')}
    test_spec=make_test_spec(record)
    model_name='deepswe-experiment'
    repository_root=task.workdir if isinstance(getattr(task,'workdir',None),str) else '/testbed'
    prediction={'instance_id':task.task_id,'model_name_or_path':model_name,
                'model_patch':Path(patch).read_text(encoding='utf-8')}
    output_dir=Path(output_dir)
    log_root=output_dir/'harness'
    log_root.mkdir(parents=True,exist_ok=True)
    log_dir=log_root/run_id/model_name/task.task_id
    with _SWEBENCH_HARNESS_LOCK:
        original_write_text=Path.write_text
        original_log_dir=run_evaluation.RUN_EVALUATION_LOG_DIR
        def write_text_lf(path,data,encoding=None,errors=None,newline=None):
            return original_write_text(path,data,encoding=encoding,errors=errors,newline='\n')
        Path.write_text=write_text_lf
        run_evaluation.RUN_EVALUATION_LOG_DIR=log_root
        try:
            limits=benchmark_container_limits(task.metadata)
            restricted_client=_restricted_docker_client(client,limits)
            result=run_instance(test_spec,prediction,restricted_client,run_id,timeout=timeout)
        finally:
            Path.write_text=original_write_text
            run_evaluation.RUN_EVALUATION_LOG_DIR=original_log_dir
    report_path=log_dir/'report.json'
    if report_path.is_file():
        report=read_json(report_path)
        instance=report.get(task.task_id,{}) if isinstance(report,dict) else {}
        if isinstance(instance,dict) and not instance.get('patch_successfully_applied'):
            log_text=_read_harness_logs(log_dir)
            if _has_go_compile_failure(log_dir):
                return {'_error':'fail_test','_detail':log_text[-3000:] or 'Go build failed after applying the patch'}
            if _has_go_candidate_startup_panic(log_dir):
                return {'_error':'fail_test','_detail':log_text[-3000:] or 'Go package panicked before tests ran'}
            if _has_go_candidate_setup_failure(log_dir,patch,repository_root):
                return {'_error':'fail_test','_detail':log_text[-3000:] or 'Go setup failed in patched source'}
            if instance.get('infra_failure'):
                return report
            return {'_error':_classify_harness_error(log_text,_has_terminal_patch_rejection(log_dir)),
                    '_detail':log_text[-3000:] or 'SWE-bench produced no parseable test results'}
        return report
    log_text=_read_harness_logs(log_dir)
    if _has_go_compile_failure(log_dir):
        return {'_error':'fail_test','_detail':log_text[-3000:] or 'Go build failed after applying the patch'}
    if _has_go_candidate_startup_panic(log_dir):
        return {'_error':'fail_test','_detail':log_text[-3000:] or 'Go package panicked before tests ran'}
    if _has_go_candidate_setup_failure(log_dir,patch,repository_root):
        return {'_error':'fail_test','_detail':log_text[-3000:] or 'Go setup failed in patched source'}
    return {'_error':_classify_harness_error(log_text,_has_terminal_patch_rejection(log_dir)),
            '_detail':log_text[-3000:] or str(result)}


def run_swebench_patch(backend,task,image,patch,output,protocol_hash=''):
    from ..results.storage import digest

    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    source=task.benchmark
    run_id='deepswe-'+digest(str(output.resolve())+uuid.uuid4().hex)[:20]
    try:
        report=_run_official_harness(
            task=task,image=image,patch=patch,client=backend.client,run_id=run_id,output_dir=output,
            dataset_revision=source['dataset_revision'],harness_revision=source['harness_revision'],timeout=1800,
        )
        if isinstance(report,dict) and report.get('_error'):
            result={'status':report['_error'],'reward':None if report['_error'] in ('timeout','infrastructure_error') else 0,
                    'detail':report.get('_detail','')}
        else:
            result=parse_swebench_report(report,task.task_id)
    except Exception as exc:
        result={'status':'infrastructure_error','reward':None,
                'detail':f'{type(exc).__name__}: {str(exc)[:1000]}'}
    result.update(schema_version=1,protocol_hash=protocol_hash)
    result['harness_run_id']=run_id
    write_json(output/'result.json',result)
    return result

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

def _classify_patcheval_error(error_log: str, language: str) -> tuple[str, str]:
    """Classify PatchEval non-zero fix-run.sh exit using official PatchEval heuristics."""
    if "patch does not apply" in error_log or "error: patch failed" in error_log:
        return "apply_failure", "apply_fail"
    lang = (language or "").strip().lower()
    if lang == "python":
        if "SyntaxError" in error_log or "IndentationError" in error_log:
            return "fail_test", "compilation_fail"
        return "fail_test", "validation_fail"
    if lang in ("javascript", "js", "typescript", "ts"):
        if "SyntaxError" in error_log or "TypeError" in error_log:
            return "fail_test", "compilation_fail"
        return "fail_test", "validation_fail"
    if lang == "go":
        if re.search(r"^.*\.go:\d+:\d+: ", error_log, re.MULTILINE) and not re.search(r"panic:", error_log):
            return "fail_test", "compilation_fail"
        return "fail_test", "validation_fail"
    return "fail_test", "validation_fail"


def run_patcheval_patch(backend, task, image, patch, output, protocol_hash="", timeout=600):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    patch_bytes = Path(patch).read_bytes().replace(b"\r\n", b"\n")
    if patch_bytes and not patch_bytes.endswith(b"\n"):
        patch_bytes += b"\n"
    rc = -1
    stdout = ""
    try:
        with backend.sandbox(image, task, workdir="/workspace") as box:
            box.put_bytes("/workspace/fix.patch", patch_bytes)
            rc, stdout = box.execute("bash -lc 'cd /workspace && bash fix-run.sh'", timeout)
        (output / "test-stdout.txt").write_text(stdout, encoding="utf-8")
        if rc == 0:
            result = {
                "status": "pass",
                "reward": 1,
                "benchmark": {"validation_type": "Repair Success", "language": task.language, "exit_code": rc},
            }
        elif rc in (124, 137):
            result = {
                "status": "timeout",
                "reward": None,
                "benchmark": {"validation_type": "timeout", "language": task.language, "exit_code": rc},
                "detail": stdout[-2000:],
            }
        else:
            status, val_type = _classify_patcheval_error(stdout, task.language)
            result = {
                "status": status,
                "reward": 0,
                "benchmark": {"validation_type": val_type, "language": task.language, "exit_code": rc},
                "detail": stdout[-2000:],
            }
    except Exception as exc:
        (output / "test-stdout.txt").write_text(stdout, encoding="utf-8")
        result = {
            "status": "infrastructure_error",
            "reward": None,
            "benchmark": {"validation_type": "infrastructure_error", "language": task.language, "exit_code": rc},
            "detail": f"{type(exc).__name__}: {str(exc)[:1000]}",
        }
    result.update(schema_version=1, protocol_hash=protocol_hash)
    write_json(output / "result.json", result)
    return result


def evaluate_patch(backend,task,image,patch,output,protocol_hash=''):
    from ..tasks.tasks import is_patcheval_task
    if is_patcheval_task(task):
        return run_patcheval_patch(backend,task,image,patch,output,protocol_hash)
    if task.benchmark:
        return run_swebench_patch(backend,task,image,patch,output,protocol_hash)
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
    from ..tasks.tasks import is_patcheval_task
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    if is_patcheval_task(task):
        result={'schema_version':1,'protocol_hash':protocol_hash,'status':'not_applicable',
                'passed':0,'failed':0,'total':len(manifest.get('kept_node_ids',[]))}
        write_json(output/'result.json',result); return result
    if task.benchmark:
        return _evaluate_go_tests(backend,task,image,snapshot,manifest,patch,output,timeout,protocol_hash)
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


def _evaluate_go_tests(backend,task,image,snapshot,manifest,patch,output,timeout,protocol_hash):
    from ..execution.preparation import clean

    count=len(manifest.get('kept_node_ids',[]))
    if count==0:
        result={'schema_version':1,'protocol_hash':protocol_hash,'status':'not_applicable',
                'passed':0,'failed':0,'total':0}
        write_json(output/'result.json',result)
        return result
    stdout=''
    try:
        with backend.sandbox(image,task) as box:
            box.checked(f'find {shlex.quote(box.cwd)} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +')
            box.put_tree(snapshot,box.cwd)
            clean(box)
            patch_data=Path(patch).read_bytes().replace(b'\r\n',b'\n')
            if patch_data:
                box.put_bytes('/tmp/model.patch',patch_data)
                box.checked(f'git -C {shlex.quote(box.cwd)} apply --check /tmp/model.patch && git -C {shlex.quote(box.cwd)} apply /tmp/model.patch')
            package=manifest.get('test_package') or (task.benchmark.get('task_options',{}) if isinstance(task.benchmark,dict) else {}).get('test_package','.')
            rc,stdout=run_selected_go_tests(box,manifest,timeout,package=package)
        (output/'stdout.txt').write_text(stdout,encoding='utf-8')
        names=[group.rsplit('::',1)[-1] for group in manifest['kept_groups']]
        test_results=parse_go_test_results(stdout,names)
        passed=sum(status=='pass' for status in test_results.values())
        failed=sum(status=='fail' for status in test_results.values())
        skipped=sum(status=='skip' for status in test_results.values())
        timed_out=rc in (124,137)
        if timed_out:
            status='timeout'
        elif failed:
            status='fail_test'
        elif rc==0 and len(test_results)==count and passed+skipped==count:
            status='pass' if skipped==0 else 'partial'
        else:
            status='infrastructure_error'
        result={'schema_version':1,'protocol_hash':protocol_hash,'status':status,
                'passed':passed,'failed':failed,'skipped':skipped,'total':count,'exit_code':rc}
    except Exception as exc:
        (output/'stdout.txt').write_text(stdout,encoding='utf-8')
        result={'schema_version':1,'protocol_hash':protocol_hash,
                'status':'apply_failure' if 'git' in str(exc).lower() and 'apply' in str(exc).lower() else 'infrastructure_error',
                'passed':0,'failed':0,'total':count,'detail':f'{type(exc).__name__}: {str(exc)[:1000]}'}
    write_json(output/'result.json',result)
    return result

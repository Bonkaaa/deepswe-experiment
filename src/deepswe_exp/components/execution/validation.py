from __future__ import annotations
import hashlib
from pathlib import Path
from ..tasks.tasks import load_task,verify_benchmark_record
from ..results.storage import read_json,write_json,digest
from ..evaluation.verifier import evaluate_patch

_TRANSIENT_BENCHMARK_FAILURES={'timeout','infrastructure_error'}


def _evaluate_swebench_with_retries(backend,task,image,patch,folder,protocol_hash=''):
    folder=Path(folder)
    result_path=folder/'result.json'
    if result_path.exists():
        cached=read_json(result_path)
        if (cached.get('status') not in _TRANSIENT_BENCHMARK_FAILURES
                and cached.get('protocol_hash','')==protocol_hash):
            return cached
    result=None
    for _ in range(3):
        result=evaluate_patch(backend,task,image,patch,folder,protocol_hash)
        if result.get('status') not in _TRANSIENT_BENCHMARK_FAILURES:
            break
    return result


def validate_task(settings,task_id,backend):
    task=load_task(settings.task_path(task_id)); root=settings.output_root/task_id
    prepared=read_json(root/'prepared.json'); records={}
    target=root/'validation'
    if task.benchmark:
        verify_benchmark_record(task,prepared)
        return _validate_swebench_task(settings,task,prepared,backend,target)
    for kind in ('base','reference'):
        records[kind]=[]
        for repeat in range(3):
            folder=target/kind/str(repeat)
            if (folder/'result.json').exists(): result=read_json(folder/'result.json')
            else:
                folder.mkdir(parents=True,exist_ok=True); patch=folder/'input.patch'
                patch.write_bytes(b'' if kind=='base' else (task.path/'solution/solution.patch').read_bytes().replace(b'\r\n',b'\n'))
                result=evaluate_patch(backend,task,prepared['verifier_image'],patch,folder)
            records[kind].append(result)
    expected=all(r['status']=='pass' and r['reward']==1 for r in records['reference'])
    expected &= all(r['status']=='fail_test' and r.get('benchmark',{}).get('p2p_passed')==r.get('benchmark',{}).get('p2p_total') and r.get('benchmark',{}).get('f2p_passed',0)<r.get('benchmark',{}).get('f2p_total',0) for r in records['base'])
    stable=True
    for kind in records:
        metrics=[(r['status'],r.get('benchmark')) for r in records[kind]]
        stable &= all(m==metrics[0] for m in metrics)
        # Compare every node, not just aggregate scores.
        reports=[read_json(target/kind/str(i)/'verifier/ctrf.json')['results']['tests'] for i in range(3) if (target/kind/str(i)/'verifier/ctrf.json').exists()]
        stable &= len(reports)==3 and all(rows==reports[0] for rows in reports)
    validation_hash=digest({'prepared':prepared,'records':records,'stable':stable})
    value={'schema_version':1,'protocol_hash':validation_hash,
           'valid':bool(expected and stable),'records':records,'stable':stable,'prepared':prepared}
    write_json(target/'validation.json',value)
    return value


def _validate_swebench_task(settings,task,prepared,backend,target):
    target.mkdir(parents=True,exist_ok=True)
    prepared_hash=digest(prepared)
    patch_hashes={}
    records={}
    for kind in ('base','reference'):
        records[kind]=[]
        for repeat in range(3):
            folder=target/kind/str(repeat)
            folder.mkdir(parents=True,exist_ok=True)
            patch=folder/'input.patch'
            reference=task.metadata['patch'].replace('\r\n','\n') if kind=='reference' else ''
            patch_bytes=reference.encode('utf-8')
            patch.write_bytes(patch_bytes)
            patch_hash=hashlib.sha256(patch_bytes).hexdigest()
            patch_hashes[kind]=patch_hash
            evaluation_hash=digest({'prepared_hash':prepared_hash,'patch_sha256':patch_hash})
            result=_evaluate_swebench_with_retries(
                backend,task,prepared['verifier_image'],patch,folder,evaluation_hash,
            )
            records[kind].append(result)

    expected=all(result['status']=='pass' and result['reward']==1 for result in records['reference'])
    expected &= all(
        result['status']=='fail_test'
        and result.get('benchmark',{}).get('p2p_passed')==result.get('benchmark',{}).get('p2p_total')
        and result.get('benchmark',{}).get('f2p_passed',0)<result.get('benchmark',{}).get('f2p_total',0)
        for result in records['base']
    )
    stable=True
    for results in records.values():
        metrics=[(result['status'],result.get('benchmark')) for result in results]
        stable &= all(metric==metrics[0] for metric in metrics)
    validation_hash=digest({'prepared_hash':prepared_hash,'patch_hashes':patch_hashes,
                             'records':records,'stable':stable})
    value={'schema_version':1,'protocol_hash':validation_hash,'valid':bool(expected and stable),
           'records':records,'stable':stable,'prepared_hash':prepared_hash,
           'patch_hashes':patch_hashes,'prepared':prepared}
    write_json(target/'validation.json',value)
    return value

from __future__ import annotations
from pathlib import Path
from ..tasks.tasks import load_task
from ..results.storage import read_json,write_json,digest
from ..evaluation.verifier import evaluate_patch

def validate_task(settings,task_id,backend):
    task=load_task(settings.task_path(task_id)); root=settings.output_root/task_id
    prepared=read_json(root/'prepared.json'); records={}
    target=root/'validation'
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

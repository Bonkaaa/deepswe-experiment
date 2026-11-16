from __future__ import annotations
import json
import shutil
from pathlib import Path
from ..tasks.tasks import load_task
from ..tasks.variants import inventory,choose,filter_snapshot,source_hash,snapshot_hash,MissingRelevanceScores
from ..results.storage import write_json,read_json,digest,scratch_directory
from .docker_env import checkout_base

PLUGIN=Path(__file__).parents[1]/'tasks/collection_plugin.py'

def collect(box,temp_parent=None):
    box.put_bytes('/tmp/experiment_collection.py',PLUGIN.read_bytes())
    rc,out=box.execute('PYTHONPATH=/tmp python -m pytest tests --collect-only -q -p experiment_collection',600)
    if rc: raise RuntimeError('pytest collection failed: '+out[-2000:])
    with scratch_directory(temp_parent or Path.cwd()) as tmp:
        box.get_tree('/tmp/collection.json',tmp)
        return read_json(tmp/'collection.json')

def clean(box):
    # Trusted setup executes before giving the unprivileged repair agent any access.
    box.checked("rm -rf /tests /solution /tmp/* /root/.cache /app/.git && find /app -type d \\( -name __pycache__ -o -name .pytest_cache \\) -prune -exec rm -rf {} + && git init -q && git add -f . && git -c user.name=Harness -c user.email=harness@local commit -qm baseline")
    box.checked('id -u experiment >/dev/null 2>&1 || useradd -m -u 10001 experiment')
    box.checked('chown -R 10001:10001 /app')
    box.user='10001'
    rc,out=box.execute('test ! -e /tests && test ! -e /solution && test -z "$(git remote)"')
    if rc: raise RuntimeError('Isolation audit failed')

def export_app(box,dest):
    # Archive excludes Git and generated caches, not untracked source files.
    box.checked("tar --exclude='./.git' --exclude='*/__pycache__' --exclude='*/.pytest_cache' -cf /tmp/app-snapshot.tar -C /app .")
    Path(dest).parent.mkdir(parents=True,exist_ok=True)
    with scratch_directory(Path(dest).parent) as tmp:
        from .docker_env import safe_extract
        box.get_tree('/tmp/app-snapshot.tar',tmp)
        safe_extract((tmp/'app-snapshot.tar').read_bytes(),dest)
    box.checked('rm -f /tmp/app-snapshot.tar')

def prepare(settings,task_id,backend):
    task=load_task(settings.task_path(task_id)); settings.output_root.mkdir(parents=True,exist_ok=True); root=settings.output_root/task_id
    if (root/'prepared.json').exists():
        old=read_json(root/'prepared.json')
        if old['source_config_hash']!=digest({'branches':settings.config['branches'],'seeds':settings.seeds,'task_config':settings.config.get('task_options',{}).get(task_id,{})}):
            raise ValueError('Prepared configuration changed; use a new study_id')
        inventory_record=read_json(root/'inventory.json')
        if inventory_record.get('source_hash')!=old.get('source_hash'):
            raise ValueError('Prepared inventory source hash mismatch; use a new study_id')
        inventory_hash=digest({'groups':inventory_record.get('groups'),
                               'all_nodes':inventory_record.get('all_nodes'),
                               'source_hash':inventory_record.get('source_hash'),
                               'snapshot_hash':inventory_record.get('snapshot_hash')})
        if inventory_record.get('inventory_hash')!=inventory_hash:
            raise ValueError('Prepared test inventory was modified; use a new study_id')
        if snapshot_hash(root/'base')!=inventory_record.get('snapshot_hash'):
            raise ValueError('Prepared base snapshot was modified; use a new study_id')
        for key,expected_hash in old['manifests'].items():
            branch,seed=key.rsplit('/',1); variant=root/'prepared'/branch/seed
            manifest=read_json(variant/'manifest.json'); claim=manifest.pop('manifest_hash',None)
            if claim!=expected_hash or digest(manifest)!=claim:
                raise ValueError(f'Prepared manifest is damaged: {key}')
            if source_hash(variant/'snapshot',manifest['product_paths'])!=old['source_hash']:
                raise ValueError(f'Prepared source snapshot changed: {key}')
            if snapshot_hash(variant/'snapshot')!=manifest.get('snapshot_hash'):
                raise ValueError(f'Prepared snapshot changed: {key}')
        return old
    image=backend.resolve(task.image); verifier=backend.verifier_image(task,image)
    options=settings.config.get('task_options',{}).get(task_id,{})
    prefixes=options.get('product_paths',['src/adaptix/'])
    with backend.sandbox(image,task) as box:
        checkout_base(box,task)
        rows=collect(box,settings.output_root)
        groups=inventory(rows,options.get('scope_part','morphing'))
        clean(box)
        base=root/'base'; shutil.rmtree(base,ignore_errors=True); export_app(box,base)
    original_hash=source_hash(base,prefixes)
    base_snapshot_hash=snapshot_hash(base)
    write_json(root/'inventory.json',{'schema_version':1,'inventory_hash':digest({'groups':groups,'all_nodes':rows,'source_hash':original_hash,
                                                                                  'snapshot_hash':base_snapshot_hash}),
                                      'snapshot_hash':base_snapshot_hash,
                                      'groups':groups,'all_nodes':rows,'source_hash':original_hash})
    manifests={}
    for seed in settings.seeds:
        for branch in settings.config['branches']:
            try: manifest=choose(groups,branch,seed,options.get('relevance_scores'))
            except MissingRelevanceScores:
                if branch=='50-related': continue
                raise
            key=f'{branch}/{seed}'; dest=root/'prepared'/branch/str(seed)
            shutil.rmtree(dest,ignore_errors=True)
            dest.mkdir(parents=True,exist_ok=True)
            shutil.copytree(base,dest/'snapshot')
            filter_snapshot(dest/'snapshot',manifest)
            manifest.update(source_hash=original_hash,product_paths=prefixes,
                            snapshot_hash=snapshot_hash(dest/'snapshot'))
            # Recompute after attaching provenance.
            manifest.pop('manifest_hash'); manifest['manifest_hash']=digest(manifest)
            if source_hash(dest/'snapshot',prefixes)!=original_hash: raise RuntimeError('Product source changed during test filtering')
            with backend.sandbox(image,task) as box:
                checkout_base(box,task)
                box.checked('find /app -mindepth 1 -maxdepth 1 -exec rm -rf {} +')
                box.put_tree(dest/'snapshot','/app'); actual=collect(box,settings.output_root)
                expected={r['node_id'] for r in rows}-{*manifest['removed_node_ids']}
                if {r['node_id'] for r in actual}!=expected: raise RuntimeError('Filtered collection does not match manifest')
                clean(box)
                audit(box,manifest)
            write_json(dest/'manifest.json',manifest); manifests[key]=manifest['manifest_hash']
    result={'schema_version':1,'task_id':task_id,'base_commit':task.base_commit,'image':image,'verifier_image':verifier,
            'source_hash':original_hash,'manifests':manifests,'category':task.metadata['metadata'].get('category'),
            'role':options.get('role','experiment'),
            'source_config_hash':digest({'branches':settings.config['branches'],'seeds':settings.seeds,'task_config':options})}
    write_json(root/'prepared.json',result)
    return result

def audit(box,manifest):
    targets=[[g['file'],g.get('class'),g['function'],g['id']] for g in manifest['groups'] if g['id'] not in manifest['kept_groups']]
    script=("import ast,pathlib,json\n"
            f"for f,c,fn,gid in json.loads({json.dumps(json.dumps(targets))}):\n"
            " p=pathlib.Path('/app')/f\n"
            " if not p.exists(): continue\n"
            " tree=ast.parse(p.read_text())\n parent=tree\n"
            " if c: parent=next((n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==c),None)\n"
            " if parent is not None:\n"
            "  assert not any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn for n in parent.body),gid\n")
    import shlex
    box.checked('python -c '+shlex.quote(script))
    box.checked('test "$(git rev-list --all --count)" = 1')

def create_agent(backend,task,prepared,snapshot,manifest):
    box=backend.sandbox(prepared['image'],task)
    try:
        box.checked('find /app -mindepth 1 -maxdepth 1 -exec rm -rf {} +')
        box.put_tree(snapshot,'/app'); clean(box); audit(box,manifest)
        return box
    except Exception:
        box.close(); raise

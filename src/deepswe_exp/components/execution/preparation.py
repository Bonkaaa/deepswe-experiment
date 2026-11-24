from __future__ import annotations
import json
import shlex
import shutil
from pathlib import Path
from ..tasks.tasks import load_task,benchmark_record_digest
from ..tasks.go_tests import collect_go_tests,filter_go_tests
from ..tasks.variants import inventory,choose,filter_snapshot,source_hash,snapshot_hash,MissingRelevanceScores
from ..results.storage import write_json,read_json,digest,scratch_directory
from .docker_env import BENCHMARK_CONTAINER_POLICY,benchmark_container_limits,checkout_base

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
    cwd=shlex.quote(box.cwd)
    box.checked(f"rm -rf /tests /solution /tmp/* /root/.cache {cwd}/.git && find {cwd} -type d \\( -name __pycache__ -o -name .pytest_cache \\) -prune -exec rm -rf {{}} + && git -C {cwd} init -q && git -C {cwd} add -f . && git -C {cwd} -c user.name=Harness -c user.email=harness@local commit -qm baseline")
    box.checked('id -u experiment >/dev/null 2>&1 || useradd -m -u 10001 experiment')
    box.checked(f'chown -R 10001:10001 {cwd}')
    box.user='10001'
    rc,out=box.execute('test ! -e /tests && test ! -e /solution && test -z "$(git remote)"')
    if rc: raise RuntimeError('Isolation audit failed')

def export_app(box,dest):
    # Archive excludes Git and generated caches, not untracked source files.
    cwd=shlex.quote(box.cwd)
    box.checked(f"tar --exclude='./.git' --exclude='*/__pycache__' --exclude='*/.pytest_cache' -cf /tmp/app-snapshot.tar -C {cwd} .")
    Path(dest).parent.mkdir(parents=True,exist_ok=True)
    with scratch_directory(Path(dest).parent) as tmp:
        from .docker_env import safe_extract
        box.get_tree('/tmp/app-snapshot.tar',tmp)
        safe_extract((tmp/'app-snapshot.tar').read_bytes(),dest)
    box.checked('rm -f /tmp/app-snapshot.tar')

def prepare(settings,task_id,backend):
    task=load_task(settings.task_path(task_id))
    if task.benchmark:
        return _prepare_swebench(settings,task,backend)
    settings.output_root.mkdir(parents=True,exist_ok=True); root=settings.output_root/task_id
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
        cwd=shlex.quote(box.cwd)
        box.checked(f'find {cwd} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +')
        box.put_tree(snapshot,box.cwd); clean(box)
        if task.benchmark:
            audit_go(box,task,manifest)
        else:
            audit(box,manifest)
        return box
    except Exception:
        box.close(); raise


def verify_prepared_variant(task_root,prepared,branch,seed):
    key=f'{branch}/{seed}'
    expected=prepared.get('manifests',{}).get(key)
    if not expected:
        raise ValueError(f'Prepared manifest is missing: {key}')
    variant=Path(task_root)/'prepared'/branch/str(seed)
    manifest=read_json(variant/'manifest.json')
    claim=manifest.get('manifest_hash')
    payload={name:value for name,value in manifest.items() if name!='manifest_hash'}
    if claim!=expected or digest(payload)!=claim:
        raise ValueError(f'Prepared manifest is damaged: {key}')
    snapshot=variant/'snapshot'
    if snapshot_hash(snapshot)!=manifest.get('snapshot_hash'):
        raise ValueError(f'Prepared snapshot changed: {key}')
    if source_hash(snapshot,manifest.get('product_paths',[]))!=prepared.get('source_hash'):
        raise ValueError(f'Prepared product source changed: {key}')
    return manifest


def _prepare_swebench(settings,task,backend):
    settings.output_root.mkdir(parents=True,exist_ok=True)
    root=settings.output_root/task.task_id
    options=settings.config.get('task_options',{}).get(task.task_id,{})
    pattern=options.get('test_pattern','^Test.*Router')
    package=options.get('test_package','.')
    prefixes=options.get('product_paths',[])
    instance_hash=benchmark_record_digest(task)
    grader_policy={**BENCHMARK_CONTAINER_POLICY,**benchmark_container_limits(task.metadata)}
    source_config_hash=digest({'branches':settings.config['branches'],'seeds':settings.seeds,
                               'task_config':options,'benchmark':task.benchmark,
                               'benchmark_instance_hash':instance_hash,
                               'grader_container_policy':grader_policy})
    if (root/'prepared.json').exists():
        old=read_json(root/'prepared.json')
        if old.get('source_config_hash')!=source_config_hash:
            raise ValueError('Prepared benchmark configuration changed; use a new study_id')
        from ..tasks.tasks import verify_benchmark_record
        verify_benchmark_record(task,old)
        inventory_record=read_json(root/'inventory.json')
        if snapshot_hash(root/'base')!=inventory_record.get('snapshot_hash'):
            raise ValueError('Prepared benchmark base snapshot changed; use a new study_id')
        for key in old['manifests']:
            branch,seed=key.rsplit('/',1)
            verify_prepared_variant(root,old,branch,seed)
        return old

    base_image=backend.resolve(task.image)
    image=backend.prepare_swebench_image(task,base_image)
    with backend.sandbox(image,task) as box:
        checkout_base(box,task)
        rows=collect_go_tests(box,pattern,package=package)
        clean(box)
        base=root/'base'
        shutil.rmtree(base,ignore_errors=True)
        export_app(box,base)

        original_hash=source_hash(base,prefixes)
        base_hash=snapshot_hash(base)
        groups=inventory(rows,scope_part=None)
        inventory_value={'schema_version':1,'groups':groups,'all_nodes':rows,
                         'source_hash':original_hash,'snapshot_hash':base_hash}
        inventory_value['inventory_hash']=digest(inventory_value)
        write_json(root/'inventory.json',inventory_value)
        manifests={}
        for seed in settings.seeds:
            for branch in settings.config['branches']:
                manifest=choose(groups,branch,seed)
                manifest['test_pattern']=pattern
                manifest['test_package']=package
                variant=root/'prepared'/branch/str(seed)
                shutil.rmtree(variant,ignore_errors=True)
                variant.mkdir(parents=True,exist_ok=True)
                box.user='root'
                box.checked(f'find {shlex.quote(box.cwd)} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +')
                box.put_tree(base,box.cwd)
                filter_go_tests(box,manifest,pattern,box.cwd,package=package)
                clean(box)
                audit_go(box,task,manifest)
                export_app(box,variant/'snapshot')
                manifest.update(source_hash=original_hash,product_paths=prefixes,test_pattern=pattern,
                                test_package=package,snapshot_hash=snapshot_hash(variant/'snapshot'))
                manifest.pop('manifest_hash')
                manifest['manifest_hash']=digest(manifest)
                if source_hash(variant/'snapshot',prefixes)!=original_hash:
                    raise RuntimeError('Product source changed while filtering Go tests')
                write_json(variant/'manifest.json',manifest)
                manifests[f'{branch}/{seed}']=manifest['manifest_hash']

    result={'schema_version':1,'task_id':task.task_id,'base_commit':task.base_commit,
            'base_image':base_image,'image':image,'verifier_image':image,'source_hash':original_hash,
            'manifests':manifests,'category':task.metadata.get('category'),
            'role':options.get('role','experiment'),'source_config_hash':source_config_hash,
            'benchmark_instance_hash':instance_hash,'grader_container_policy':grader_policy,
            'harness_revision':task.benchmark['harness_revision'],'benchmark':task.benchmark}
    write_json(root/'prepared.json',result)
    return result


def audit_go(box,task,manifest):
    options=task.benchmark.get('task_options',{})
    pattern=manifest.get('test_pattern') or options.get('test_pattern','^Test.*Router')
    package=manifest.get('test_package') or options.get('test_package','.')
    actual=collect_go_tests(box,pattern,allow_empty=True,package=package)
    expected=sorted(group.rsplit('::',1)[-1] for group in manifest['kept_groups'])
    found=sorted(row['function'] for row in actual)
    if found!=expected:
        raise RuntimeError(f'Filtered Go test inventory differs from manifest: expected {expected}, found {found}')
    cwd=shlex.quote(box.cwd)
    box.checked(f'test "$(git -C {cwd} rev-list --all --count)" = 1')

from __future__ import annotations
import ast
import hashlib
import math
import random
from pathlib import Path
from typing import Iterable,Sequence
from ..results.storage import digest,write_json
from ..results.schemas import Manifest,TestGroup

BRANCH_RATIOS={'100':1.,'70-random':.7,'50-random':.5,'30-random':.3,'0':0.}

class MissingRelevanceScores(ValueError): pass

def source_hash(root,prefixes=('src/adaptix',)):
    rows={}
    for prefix in prefixes:
        path=Path(root)/prefix
        files=[path] if path.is_file() else sorted(path.rglob('*'))
        for p in files:
            if p.is_file() and '__pycache__' not in p.parts:
                rows[p.relative_to(root).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    if not rows: raise ValueError('No product source found')
    return digest(rows)

def snapshot_hash(root):
    root=Path(root); rows={}
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if any(part in ('.git','__pycache__','.pytest_cache') for part in relative.parts): continue
        if path.is_symlink(): raise ValueError(f'Symlink in snapshot: {relative.as_posix()}')
        if path.is_file(): rows[relative.as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    if not rows: raise ValueError('Empty snapshot')
    return digest(rows)

def inventory(rows:Iterable[dict],scope_part='morphing')->list[TestGroup]:
    groups={}
    for row in rows:
        if scope_part is not None and scope_part not in Path(row['file']).parts: continue
        key=row['file']+'::'+((row.get('class')+'::') if row.get('class') else '')+row['function']
        g=groups.setdefault(key,{'id':key,'file':row['file'],'class':row.get('class'),
                                 'function':row['function'],'node_ids':[],
                                 'fixtures':sorted(set(row.get('fixtures',()))),
                                 'description':row.get('description')})
        g['node_ids'].append(row['node_id'])
        g['fixtures']=sorted(set(g['fixtures'])|set(row.get('fixtures',())))
    if not groups: raise ValueError('No tests found in scope')
    return sorted(groups.values(),key=lambda g:g['id'])

def choose(groups:Sequence[TestGroup],branch:str,seed:int,scores=None)->Manifest:
    if branch not in (*BRANCH_RATIOS,'50-related'): raise ValueError(f'Unsupported branch: {branch}')
    ordered=sorted(groups,key=lambda g:g['id']); random.Random(seed).shuffle(ordered)
    total=sum(len(g['node_ids']) for g in groups)
    ratio=BRANCH_RATIOS.get(branch,.5)
    candidates=[(0,0)]
    for i,g in enumerate(ordered): candidates.append((i+1,candidates[-1][1]+len(g['node_ids'])))
    count,n=min(candidates,key=lambda v:(abs(v[1]-ratio*total),v[1]))
    selected=ordered[:count]
    relevance_scores=None
    if branch=='50-related':
        if scores is None or not isinstance(scores,dict) or any(g['id'] not in scores for g in groups):
            raise MissingRelevanceScores('Relevance scores required before planning related branch')
        if set(scores)-{g['id'] for g in groups}:
            raise ValueError('Relevance scores contain unknown test group IDs')
        relevance_scores={}
        for group in groups:
            value=scores[group['id']]
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(float(value)):
                raise ValueError(f'Invalid relevance score for {group["id"]}')
            relevance_scores[group['id']]=float(value)
        dp={0:(0,())}
        for g in sorted(groups,key=lambda g:g['id']):
            weight=len(g['node_ids'])
            for size,(score,ids) in list(dp.items())[::-1]:
                new=size+weight
                if new>n: continue
                candidate=(score+float(scores[g['id']]),ids+(g['id'],))
                old=dp.get(new)
                if old is None or candidate[0]>old[0] or (candidate[0]==old[0] and candidate[1]<old[1]): dp[new]=candidate
        selected=[g for g in groups if g['id'] in dp[n][1]]
    keep=sorted(g['id'] for g in selected)
    payload={'schema_version':1,'branch':branch,'seed':seed,'groups':groups,'kept_groups':keep,
             'kept_node_ids':sorted(node for g in selected for node in g['node_ids']),
             'removed_node_ids':sorted(node for g in groups if g['id'] not in keep for node in g['node_ids']),
             'total_cases':total,'kept_cases':n,'actual_ratio':n/total,'target_ratio':ratio}
    if relevance_scores is not None:
        payload['relevance_scores']=dict(sorted(relevance_scores.items()))
        payload['relevance_ranking']=sorted(relevance_scores,key=lambda group:(-relevance_scores[group],group))
        payload['selected_relevance_score']=sum(relevance_scores[group] for group in keep)
    payload['manifest_hash']=digest(payload)
    return payload

def filter_snapshot(root,manifest):
    by_file={}
    for g in manifest['groups']:
        if g['id'] not in manifest['kept_groups']: by_file.setdefault(g['file'],[]).append(g)
    for relative,groups in by_file.items():
        path=Path(root)/relative
        text=path.read_text(encoding='utf-8'); tree=ast.parse(text); lines=text.splitlines(keepends=True)
        cuts=[]
        removed_by_class={}
        for g in groups:
            parent=tree
            if g.get('class'):
                matches=[x for x in tree.body if isinstance(x,ast.ClassDef) and x.name==g['class']]
                if len(matches)!=1 or matches[0].bases: raise ValueError('Unsafe inherited/ambiguous test class: '+g['id'])
                parent=matches[0]
            nodes=[x for x in parent.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==g['function']]
            if len(nodes)!=1: raise ValueError('Dynamic/ambiguous test: '+g['id'])
            node=nodes[0]; start=min([node.lineno]+[d.lineno for d in node.decorator_list])-1
            cuts.append((start,node.end_lineno,''))
            if g.get('class'):
                removed_by_class.setdefault(g['class'],set()).add(node.name)
        # If removing selected methods empties a test class, remove the complete
        # class declaration (including decorators) so pytest no longer collects it.
        class_ranges=[]
        for name,removed_names in removed_by_class.items():
            cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name==name)
            remaining=[x for x in cls.body if not isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) or x.name not in removed_names]
            if not remaining:
                start=min([cls.lineno]+[d.lineno for d in cls.decorator_list])-1
                cuts.append((start,cls.end_lineno,''))
                class_ranges.append((start,cls.end_lineno))
        cuts=[cut for cut in cuts if (cut[0],cut[1]) in class_ranges
              or not any(left<=cut[0] and cut[1]<=right for left,right in class_ranges)]
        for start,end,replacement in sorted(cuts,reverse=True): lines[start:end]=[replacement]
        result=''.join(lines); ast.parse(result); path.write_text(result,encoding='utf-8',newline='\n')

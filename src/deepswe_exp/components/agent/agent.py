from __future__ import annotations
import asyncio
import json
import math
import time
from pathlib import Path
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, messages_to_dict
from langchain_google_genai import ChatGoogleGenerativeAI
from ..results.storage import write_json,append_event
from ..execution.docker_env import DockerFailure
from ..results.schemas import AgentResult
from .quota import InputBudget,QuotaUnavailable

def load_system_prompt(path):
    text=Path(path).read_text(encoding='utf-8').strip()
    if not text: raise ValueError('Empty system prompt')
    return text

def build_model(settings):
    # Both per-request and client-level Google SDK retry budgets are disabled.
    return ChatGoogleGenerativeAI(model=settings.model,api_key=settings.api_key,vertexai=False,
        max_output_tokens=settings.budget['max_output_tokens'],
        client_args={'http_options':{'retry_options':{'attempts':1}}}).bind_tools([{
        'name':'run_shell','description':'Run a shell command inside the assigned Linux repository at /app.',
        'parameters':{'type':'object','properties':{'command':{'type':'string'}},'required':['command']}}])

async def run_agent(*,settings,instruction,box,output_dir,ledger,protocol_hash='',model=None,clock=time.monotonic)->AgentResult:
    model=model or build_model(settings)
    budget=settings.budget; started=clock(); deadline=started+budget['wall_time_seconds']
    messages=[SystemMessage(content=load_system_prompt(settings.prompt_path)),HumanMessage(content=instruction)]
    calls=0; stop='internal_error'; usage=[]; commands=[]
    def save():
        data={'schema_version':1,'protocol_hash':protocol_hash,'stop_reason':stop,'model_requests':calls,'elapsed_seconds':clock()-started,
              'usage':usage,'messages':messages_to_dict(messages),'commands':commands}
        # Secrets are never part of model config; redact any accidentally echoed key.
        text=settings.redact(json.dumps(data,ensure_ascii=False,default=str))
        write_json(output_dir/'trajectory.json',json.loads(text))
    try:
        while True:
            save()
            if calls>=budget['max_model_requests']: stop='request_budget'; break
            if clock()>=deadline: stop='time_budget'; break
            serialized=json.dumps(messages_to_dict(messages),ensure_ascii=False)
            estimate=math.ceil(len(serialized.encode('utf-8'))/3*1.25)+512
            ledger.reserve(estimate,deadline,clock)
            calls+=1; save()
            try:
                response=await asyncio.wait_for(model.ainvoke(messages,max_retries=0,
                    timeout=max(.01,deadline-clock())),timeout=max(.01,deadline-clock()))
            except asyncio.TimeoutError:
                stop='time_budget'; break
            except Exception as exc:
                stop='provider_error'
                append_event(output_dir/'errors.jsonl',{'type':type(exc).__name__,'message':settings.redact(str(exc))})
                break
            messages.append(response); usage.append(response.usage_metadata)
            ledger.usage(response.usage_metadata); save()
            if not response.tool_calls:
                stop='model_finished' if not response.invalid_tool_calls else 'format_error'; break
            for call in response.tool_calls:
                if clock()>=deadline: stop='time_budget'; break
                if call['name']!='run_shell' or not isinstance(call.get('args',{}).get('command'),str):
                    content='Rejected: expected run_shell(command: string)'
                else:
                    timeout=min(budget['tool_timeout_seconds'],max(.01,deadline-clock()))
                    rc,raw=await asyncio.to_thread(box.execute,call['args']['command'],timeout)
                    raw=settings.redact(raw)
                    commands.append({'command':call['args']['command'],'exit_code':rc,'output':raw})
                    append_event(output_dir/'commands.jsonl',commands[-1])
                    cap=budget['max_tool_output_chars']
                    content=f'exit_code={rc}\n'+raw[:cap]+(f'\n[TRUNCATED: {len(raw)-cap} characters omitted]' if len(raw)>cap else '')
                messages.append(ToolMessage(content=content,tool_call_id=call['id'])); save()
            if stop=='time_budget': break
    except InputBudget: stop='input_budget'
    except QuotaUnavailable: stop='quota_interrupted'
    except TimeoutError: stop='time_budget'
    except DockerFailure: stop='docker_error'
    finally:
        save()
    return {'schema_version':1,'protocol_hash':protocol_hash,'stop_reason':stop,
            'model_requests':calls,'elapsed_seconds':clock()-started,'usage':usage}

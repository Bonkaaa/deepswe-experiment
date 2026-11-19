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
from .context import build_request_context, classify_response, truncate_output

def load_system_prompt(path):
    text=Path(path).read_text(encoding='utf-8').strip()
    if not text: raise ValueError('Empty system prompt')
    return text

def build_model(settings):
    if getattr(settings, "model_provider", "google") == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=settings.model,
            base_url=settings.ollama_base_url,
            num_ctx=settings.ollama_num_ctx,
            num_predict=settings.budget["max_output_tokens"],
            temperature=float(getattr(settings, "ollama_options", {}).get("temperature", 0.0)),
        ).bind_tools([{
            'name': 'run_shell',
            'description': 'Run a shell command inside the assigned Linux repository at /app.',
            'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']}
        }])

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
    calls=0; stop='internal_error'; usage=[]; commands=[]; context_stats={}
    truncation_retries=0
    emergency_context_retry_done=False
    force_emergency_compact=False
    recovery_feedback=None
    def save():
        data={'schema_version':1,'protocol_hash':protocol_hash,'stop_reason':stop,'model_requests':calls,'elapsed_seconds':clock()-started,
              'usage':usage,'messages':messages_to_dict(messages),'commands':commands,'context_stats':context_stats}
        # Secrets are never part of model config; redact any accidentally echoed key.
        text=settings.redact(json.dumps(data,ensure_ascii=False,default=str))
        write_json(output_dir/'trajectory.json',json.loads(text))
    try:
        while True:
            save()
            if calls>=budget['max_model_requests']: stop='request_budget'; break
            if clock()>=deadline: stop='time_budget'; break

            if recovery_feedback:
                messages.append(HumanMessage(content=recovery_feedback))
                recovery_feedback = None

            input_budget = getattr(settings, "input_budget", 1_000_000)
            request_messages, ctx_stats = build_request_context(
                messages,
                input_budget=input_budget,
                force_compact=force_emergency_compact,
                min_recent_turns=2 if force_emergency_compact else 3,
            )
            force_emergency_compact = False
            context_stats = ctx_stats
            serialized=json.dumps(messages_to_dict(request_messages),ensure_ascii=False)
            estimate=math.ceil(len(serialized.encode('utf-8'))/3*1.25)+512
            ledger.reserve(estimate,deadline,clock)
            calls+=1; save()
            invoke_kwargs = {}
            if getattr(settings, "model_provider", "google") == "google":
                invoke_kwargs = {"max_retries": 0, "timeout": max(.01, deadline - clock())}
            try:
                response = await asyncio.wait_for(
                    model.ainvoke(request_messages, **invoke_kwargs),
                    timeout=max(.01, deadline - clock()),
                )
            except asyncio.TimeoutError:
                stop='time_budget'; break
            except Exception as exc:
                stop='provider_error'
                append_event(output_dir/'errors.jsonl',{'type':type(exc).__name__,'message':settings.redact(str(exc))})
                break

            meta = getattr(response, "usage_metadata", None) or {}
            messages.append(response); usage.append(meta)
            ledger.usage(meta); save()

            classification = classify_response(
                response,
                num_ctx=getattr(settings, "ollama_num_ctx", 65536),
                max_output_tokens=budget["max_output_tokens"],
                input_budget=input_budget,
            )
            kind = classification["kind"]

            if kind == "output_truncated":
                if not response.tool_calls or getattr(response, "invalid_tool_calls", None):
                    if truncation_retries < 1:
                        truncation_retries += 1
                        append_event(output_dir / 'errors.jsonl', {
                            'type': 'OutputTruncated',
                            'message': f'Single-turn output limit hit ({budget["max_output_tokens"]} tokens). Attempting 1-shot recovery retry.',
                        })
                        recovery_feedback = (
                            f"Warning: Your previous response exceeded the single-turn output token limit "
                            f"({budget['max_output_tokens']} tokens) and was truncated before completing the command. "
                            "Do NOT attempt to rewrite or dump entire large files with 'cat << EOF'. "
                            "Please make targeted, smaller edits (e.g. using 'sed -i', 'python3 -c \"...\"', or a focused patch) "
                            "to modify only the necessary lines."
                        )
                        continue
                    stop = 'output_truncated'
                    append_event(output_dir / 'errors.jsonl', {
                        'type': 'OutputTruncated',
                        'message': f'Output token limit ({budget["max_output_tokens"]} tokens) exceeded repeatedly; aborting run.',
                    })
                    break

            if kind == "context_overflow":
                if not emergency_context_retry_done:
                    emergency_context_retry_done = True
                    force_emergency_compact = True
                    append_event(output_dir / 'errors.jsonl', {
                        'type': 'ContextOverflow',
                        'message': 'Context window capacity reached; attempting emergency compact retry.',
                    })
                    messages.pop()
                    continue
                stop = 'context_overflow'
                append_event(output_dir / 'errors.jsonl', {
                    'type': 'ContextOverflow',
                    'message': f'Model input exceeded context window limit ({input_budget} tokens).',
                })
                break

            if kind == "truncation_unknown":
                if not response.tool_calls:
                    stop = 'truncation_unknown'
                    append_event(output_dir / 'errors.jsonl', {
                        'type': 'TruncationUnknown',
                        'message': 'Response truncated with length flag but inconclusive counters.',
                    })
                    break

            # Turn made valid progress or finished normally
            truncation_retries = 0

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
                    content=f'exit_code={rc}\n'+truncate_output(raw, cap, head_ratio=0.6)
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

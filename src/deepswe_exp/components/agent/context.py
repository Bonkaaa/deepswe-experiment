from __future__ import annotations

import json
import math
import re
from typing import Any
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


def truncate_output(raw: str, max_chars: int = 4000, head_ratio: float = 0.6) -> str:
    """Truncate shell/tool output using head/tail excision when exceeding max_chars.
    
    Guarantees that len(result) <= max_chars.
    """
    if len(raw) <= max_chars:
        return raw
    if max_chars <= 60:
        return raw[:max_chars]

    # Iteratively ensure marker and retained text fit precisely in max_chars
    tentative_keep = max_chars - 45
    head_chars = max(1, int(tentative_keep * head_ratio))
    tail_chars = max(1, tentative_keep - head_chars)
    omitted = len(raw) - (head_chars + tail_chars)
    marker = f"\n[... TRUNCATED {omitted} CHARS ...]\n"

    actual_keep = max_chars - len(marker)
    if actual_keep <= 0:
        return raw[:max_chars]

    head_chars = max(1, int(actual_keep * head_ratio))
    tail_chars = actual_keep - head_chars
    omitted = len(raw) - (head_chars + tail_chars)
    marker = f"\n[... TRUNCATED {omitted} CHARS ...]\n"

    return raw[:head_chars] + marker + (raw[-tail_chars:] if tail_chars > 0 else "")


def estimate_message_tokens(msg: BaseMessage) -> int:
    """Fast, conservative estimate of token count for a message."""
    content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
    # In code and dense technical text, 1 token is ~3.0 - 3.2 chars
    tokens = math.ceil(len(content.encode("utf-8", errors="replace")) / 3.0) + 8

    if isinstance(msg, AIMessage):
        if msg.tool_calls:
            try:
                calls_str = json.dumps(msg.tool_calls)
                tokens += math.ceil(len(calls_str.encode("utf-8", errors="replace")) / 3.0) + 16
            except Exception:
                tokens += 64
        if getattr(msg, "invalid_tool_calls", None):
            try:
                inv_str = json.dumps(msg.invalid_tool_calls)
                tokens += math.ceil(len(inv_str.encode("utf-8", errors="replace")) / 3.0) + 16
            except Exception:
                tokens += 64
    return tokens


def estimate_total_tokens(messages: list[BaseMessage]) -> int:
    """Estimate total tokens across a list of messages."""
    return sum(estimate_message_tokens(m) for m in messages) + 16


def group_atomic_turns(
    messages: list[BaseMessage],
) -> tuple[list[BaseMessage], list[list[BaseMessage]]]:
    """Partition message list into immutable prefix (System + initial Human) and atomic turns.
    
    Each atomic turn begins with an AIMessage and includes all subsequent ToolMessages
    answering that AIMessage's tool calls.
    """
    if not messages:
        return [], []

    prefix: list[BaseMessage] = []
    i = 0
    # Keep up to 2 initial prefix messages (SystemMessage and initial HumanMessage)
    while i < len(messages) and i < 2 and isinstance(messages[i], (SystemMessage, HumanMessage)):
        prefix.append(messages[i])
        i += 1

    turns: list[list[BaseMessage]] = []
    current_turn: list[BaseMessage] = []

    while i < len(messages):
        msg = messages[i]
        if isinstance(msg, AIMessage):
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
        else:
            if current_turn:
                current_turn.append(msg)
            else:
                prefix.append(msg)
        i += 1

    if current_turn:
        turns.append(current_turn)

    return prefix, turns


def _extract_exit_code(content: str) -> str:
    """Extract exit_code=X from tool output if present."""
    m = re.search(r"exit_code=(\d+)", content)
    return m.group(1) if m else "0"


def fold_turn_tools(turn: list[BaseMessage], max_folded_chars: int = 150) -> list[BaseMessage]:
    """Return a new turn with historical ToolMessages folded into concise summaries.
    
    Does NOT mutate original messages.
    """
    folded: list[BaseMessage] = []
    for msg in turn:
        if isinstance(msg, ToolMessage):
            orig_content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            if len(orig_content) > max_folded_chars:
                rc = _extract_exit_code(orig_content)
                new_content = (
                    f"exit_code={rc}\n"
                    f"[Tool output folded from earlier history ({len(orig_content)} chars)]"
                )
                folded.append(ToolMessage(
                    content=new_content,
                    tool_call_id=msg.tool_call_id,
                    name=getattr(msg, "name", None),
                ))
            else:
                folded.append(msg)
        else:
            folded.append(msg)
    return folded


def build_request_context(
    messages: list[BaseMessage],
    input_budget: int,
    *,
    high_watermark_ratio: float = 0.85,
    low_watermark_ratio: float = 0.65,
    min_recent_turns: int = 3,
    fold_older_than_turns: int = 4,
    force_compact: bool = False,
) -> tuple[list[BaseMessage], dict[str, Any]]:
    """Build a compacted request message list that fits within input_budget.
    
    Preserves:
    - Prefix messages (System + initial Human)
    - Full messages in the caller (never mutates in-place)
    - At least min_recent_turns of the newest turns
    - Ollama prefix cache by compacting down to low watermark only when high watermark is hit
    
    Returns:
        (request_messages, context_stats)
    """
    prefix, turns = group_atomic_turns(messages)
    total_est = estimate_total_tokens(messages)
    high_watermark = int(input_budget * high_watermark_ratio)
    low_watermark = int(input_budget * low_watermark_ratio)

    stats = {
        "original_message_count": len(messages),
        "total_turns": len(turns),
        "estimated_tokens_before": total_est,
        "input_budget": input_budget,
        "high_watermark": high_watermark,
        "low_watermark": low_watermark,
        "compacted": False,
        "folded_turns_count": 0,
        "pruned_turns_count": 0,
        "retained_turns_count": len(turns),
        "estimated_tokens_after": total_est,
    }

    # If within high watermark and not forced, keep full conversation
    if not force_compact and total_est <= high_watermark:
        return list(messages), stats

    stats["compacted"] = True

    # Tier 2: Soft compaction (folding tool outputs of older turns)
    target_budget = low_watermark if not force_compact else int(input_budget * 0.5)
    working_turns: list[list[BaseMessage]] = []
    folded_count = 0

    num_turns = len(turns)
    for idx, turn in enumerate(turns):
        # Is this turn older than recent window?
        is_historical = (num_turns - idx) > fold_older_than_turns
        if is_historical:
            folded = fold_turn_tools(turn)
            working_turns.append(folded)
            folded_count += 1
        else:
            working_turns.append(list(turn))

    stats["folded_turns_count"] = folded_count

    def _assemble(t_list: list[list[BaseMessage]]) -> list[BaseMessage]:
        out = list(prefix)
        for t in t_list:
            out.extend(t)
        return out

    assembled = _assemble(working_turns)
    current_tokens = estimate_total_tokens(assembled)

    # Tier 3: Hard atomic turn pruning (FIFO) if still exceeding target budget
    pruned_count = 0
    while len(working_turns) > min_recent_turns and current_tokens > target_budget:
        working_turns.pop(0)
        pruned_count += 1
        assembled = _assemble(working_turns)
        current_tokens = estimate_total_tokens(assembled)

    # Tier 3.5: If still exceeding target_budget, fold intermediate turns closer to the present
    if current_tokens > target_budget and len(working_turns) > 1:
        for i in range(len(working_turns) - 1):
            working_turns[i] = fold_turn_tools(working_turns[i])
        assembled = _assemble(working_turns)
        current_tokens = estimate_total_tokens(assembled)

    # Tier 3.6: If still exceeding, evict down to 1 turn (latest turn)
    while len(working_turns) > 1 and current_tokens > target_budget:
        working_turns.pop(0)
        pruned_count += 1
        assembled = _assemble(working_turns)
        current_tokens = estimate_total_tokens(assembled)

    # Tier 3.7: In extreme cases, fold the last turn as well if needed
    if current_tokens > target_budget and working_turns:
        working_turns[-1] = fold_turn_tools(working_turns[-1])
        assembled = _assemble(working_turns)
        current_tokens = estimate_total_tokens(assembled)

    stats["pruned_turns_count"] = pruned_count
    stats["retained_turns_count"] = len(working_turns)
    stats["estimated_tokens_after"] = current_tokens

    return assembled, stats


def classify_response(
    response: BaseMessage,
    *,
    num_ctx: int,
    max_output_tokens: int,
    input_budget: int,
) -> dict[str, Any]:
    """Classify model response termination cause.
    
    Returns:
        kind: 'normal' | 'output_truncated' | 'context_overflow' | 'truncation_unknown'
        prompt_tokens: int | None
        output_tokens: int | None
        total_tokens: int | None
        done_reason: str | None
        has_tool_calls: bool
    """
    resp_meta = getattr(response, "response_metadata", {}) or {}
    usage_meta = getattr(response, "usage_metadata", {}) or {}

    done_reason = resp_meta.get("done_reason") or resp_meta.get("finish_reason")
    is_length = str(done_reason).lower() in ("length", "max_tokens")

    prompt_tokens = usage_meta.get("input_tokens") or resp_meta.get("prompt_eval_count")
    output_tokens = usage_meta.get("output_tokens") or resp_meta.get("eval_count")
    total_tokens = usage_meta.get("total_tokens")
    if total_tokens is None and prompt_tokens is not None and output_tokens is not None:
        total_tokens = prompt_tokens + output_tokens

    has_tool_calls = bool(getattr(response, "tool_calls", None))

    if not is_length:
        return {
            "kind": "normal",
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "done_reason": done_reason,
            "has_tool_calls": has_tool_calls,
        }

    # If is_length:
    # 1. Output generation reached or approached max_output_tokens
    if output_tokens is not None and output_tokens >= (max_output_tokens - 16):
        # Is input context window also full?
        if total_tokens is not None and total_tokens >= (num_ctx - 64):
            kind = "context_overflow"
        else:
            kind = "output_truncated"
        return {
            "kind": kind,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "done_reason": done_reason,
            "has_tool_calls": has_tool_calls,
        }

    # 2. Total tokens reached or approached num_ctx, or prompt exceeded input_budget
    if (total_tokens is not None and total_tokens >= (num_ctx - 64)) or (
        prompt_tokens is not None and prompt_tokens >= input_budget
    ):
        return {
            "kind": "context_overflow",
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "done_reason": done_reason,
            "has_tool_calls": has_tool_calls,
        }

    # 3. Inconclusive counters
    kind = "truncation_unknown"
    return {
        "kind": kind,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "done_reason": done_reason,
        "has_tool_calls": has_tool_calls,
    }

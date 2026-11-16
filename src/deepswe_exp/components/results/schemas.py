from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


TestGroup = TypedDict("TestGroup", {
    "id": str,
    "file": str,
    "class": str | None,
    "function": str,
    "node_ids": list[str],
    "fixtures": list[str],
    "description": NotRequired[str | None],
})


class Manifest(TypedDict, total=False):
    schema_version: int
    protocol_hash: str
    manifest_hash: str
    branch: str
    seed: int
    groups: list[TestGroup]
    kept_groups: list[str]
    kept_node_ids: list[str]
    removed_node_ids: list[str]
    total_cases: int
    kept_cases: int
    actual_ratio: float
    source_hash: str
    snapshot_hash: str
    relevance_scores: dict[str, float]
    relevance_ranking: list[str]
    selected_relevance_score: float


class RunSpecification(TypedDict):
    schema_version: int
    task_id: str
    branch: str
    seed: int
    attempt_id: str
    run_id: str
    protocol_hash: str


class RunState(TypedDict):
    schema_version: int
    task_id: str
    branch: str
    seed: int
    attempt_id: str
    run_id: str
    protocol_hash: str
    status: Literal[
        "pending", "running", "interrupted", "agent_done", "scored",
        "evaluation_error", "superseded",
    ]
    group_valid: bool


class AgentResult(TypedDict):
    schema_version: int
    protocol_hash: str
    stop_reason: str
    model_requests: int
    elapsed_seconds: float
    usage: list[dict]


class EvaluationResult(TypedDict, total=False):
    schema_version: int
    protocol_hash: str
    status: Literal["pass", "fail_test", "apply_failure", "timeout", "infrastructure_error"]
    reward: int | None
    benchmark: dict
    detail: str


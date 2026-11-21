from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from minisweagent.exceptions import Submitted

from .context import truncate_output


class SandboxEnvironment:
    """Adapter connecting mini-SWE-agent to deepswe-exp's Docker Sandbox."""

    def __init__(
        self,
        box,
        cwd: str = "/app",
        timeout: int = 120,
        max_output_chars: int = 8000,
        redact_fn: Callable[[str], str] | None = None,
    ):
        self.box = box
        self.cwd = cwd
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self.redact_fn = redact_fn
        self.commands: list[dict[str, Any]] = []
        self.config = SimpleNamespace(cwd=cwd, timeout=timeout)

    def execute(self, action: dict[str, Any], cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        effective_timeout = timeout or self.timeout
        exec_cmd = f"cd {shlex.quote(cwd)} && {command}" if cwd and cwd != self.cwd else command

        rc, out = self.box.execute(exec_cmd, timeout=effective_timeout)
        raw_result = {
            "output": out,
            "returncode": rc,
            "exception_info": "",
        }
        # Check task completion token on raw output before truncation
        self._check_finished(raw_result)

        cleaned_out = self.redact_fn(out) if self.redact_fn else out
        truncated_out = truncate_output(cleaned_out, max_chars=self.max_output_chars)
        exception_info = f"Command timed out after {effective_timeout} seconds" if rc == 124 else ""

        self.commands.append({
            "command": command,
            "exit_code": rc,
            "output": truncated_out,
        })
        return {
            "output": truncated_out,
            "returncode": rc,
            "exception_info": exception_info,
        }

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return {
            "system": "Linux",
            "release": "6.1",
            "version": "1",
            "machine": "x86_64",
            "cwd": self.cwd,
            **kwargs,
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "info": {
                "config": {
                    "environment": {"cwd": self.cwd, "timeout": self.timeout},
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }

    def _check_finished(self, output: dict[str, Any]) -> None:
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and output["returncode"] == 0:
            submission = "".join(lines[1:])
            if self.redact_fn:
                submission = self.redact_fn(submission)
            raise Submitted(
                {
                    "role": "exit",
                    "content": submission,
                    "extra": {"exit_status": "Submitted", "submission": submission},
                }
            )

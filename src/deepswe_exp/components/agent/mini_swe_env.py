from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from minisweagent.exceptions import Submitted


class SandboxEnvironment:
    """Adapter connecting mini-SWE-agent to deepswe-exp's Docker Sandbox."""

    def __init__(self, box, cwd: str = "/app", timeout: int = 120):
        self.box = box
        self.cwd = cwd
        self.timeout = timeout
        self.config = SimpleNamespace(cwd=cwd, timeout=timeout)

    def execute(self, action: dict[str, Any], cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        effective_timeout = timeout or self.timeout

        rc, out = self.box.execute(command, timeout=effective_timeout)
        result = {
            "output": out,
            "returncode": rc,
            "exception_info": "",
        }
        self._check_finished(result)
        return result

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
            raise Submitted(
                {
                    "role": "exit",
                    "content": submission,
                    "extra": {"exit_status": "Submitted", "submission": submission},
                }
            )

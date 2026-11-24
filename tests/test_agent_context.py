from __future__ import annotations

import unittest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from deepswe_exp.components.agent.context import (
    build_request_context,
    estimate_message_tokens,
    estimate_total_tokens,
    fold_turn_tools,
    group_atomic_turns,
    truncate_output,
)


class TestAgentContext(unittest.TestCase):
    def test_truncate_output_short(self):
        text = "short output"
        self.assertEqual(truncate_output(text, max_chars=100), text)

    def test_truncate_output_long(self):
        text = "A" * 5000 + "B" * 5000
        result = truncate_output(text, max_chars=4000, head_ratio=0.6)
        self.assertLessEqual(len(result), 4000)
        self.assertTrue(result.startswith("A" * 100))
        self.assertTrue(result.endswith("B" * 100))
        self.assertIn("[... TRUNCATED", result)
        self.assertIn("CHARS ...]", result)

    def test_truncate_output_bounds(self):
        # Very small budget edge case
        text = "Hello world from deepswe experiment context window test"
        truncated = truncate_output(text, max_chars=20)
        self.assertLessEqual(len(truncated), 20)

    def test_estimate_message_tokens(self):
        sys_msg = SystemMessage(content="You are an agent.")
        t_sys = estimate_message_tokens(sys_msg)
        self.assertGreater(t_sys, 0)

        ai_msg = AIMessage(
            content="I will run a command",
            tool_calls=[{"name": "run_shell", "args": {"command": "cat file.py"}, "id": "call-1"}],
        )
        t_ai = estimate_message_tokens(ai_msg)
        self.assertGreater(t_ai, t_sys)

        tool_msg = ToolMessage(content="exit_code=0\n" + ("x" * 3000), tool_call_id="call-1")
        t_tool = estimate_message_tokens(tool_msg)
        self.assertGreater(t_tool, 1000)

    def test_group_atomic_turns(self):
        messages = [
            SystemMessage(content="sys prompt"),
            HumanMessage(content="instruction"),
            AIMessage(content="", tool_calls=[{"name": "run_shell", "args": {"command": "ls"}, "id": "c1"}]),
            ToolMessage(content="exit_code=0\nfile1.py\nfile2.py", tool_call_id="c1"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "run_shell", "args": {"command": "head -5 a"}, "id": "c2a"},
                    {"name": "run_shell", "args": {"command": "head -5 b"}, "id": "c2b"},
                ],
            ),
            ToolMessage(content="exit_code=0\nout a", tool_call_id="c2a"),
            ToolMessage(content="exit_code=0\nout b", tool_call_id="c2b"),
        ]
        prefix, turns = group_atomic_turns(messages)
        self.assertEqual(len(prefix), 2)
        self.assertEqual(prefix[0].content, "sys prompt")
        self.assertEqual(prefix[1].content, "instruction")

        self.assertEqual(len(turns), 2)
        # Turn 1
        self.assertEqual(len(turns[0]), 2)
        self.assertIsInstance(turns[0][0], AIMessage)
        self.assertIsInstance(turns[0][1], ToolMessage)
        # Turn 2 has 1 AIMessage and 2 ToolMessages
        self.assertEqual(len(turns[1]), 3)
        self.assertIsInstance(turns[1][0], AIMessage)
        self.assertEqual(turns[1][1].tool_call_id, "c2a")
        self.assertEqual(turns[1][2].tool_call_id, "c2b")

    def test_fold_turn_tools_immutability(self):
        orig_tool = ToolMessage(content="exit_code=0\n" + ("output data " * 50), tool_call_id="c1")
        turn = [
            AIMessage(content="", tool_calls=[{"name": "run_shell", "args": {"command": "ls"}, "id": "c1"}]),
            orig_tool,
        ]
        folded = fold_turn_tools(turn, max_folded_chars=50)

        # Original message must remain intact
        self.assertIn("output data output data", orig_tool.content)
        # Folded message must be compressed
        self.assertIn("[Tool output folded from earlier history", folded[1].content)
        self.assertIn("exit_code=0", folded[1].content)
        self.assertEqual(folded[1].tool_call_id, "c1")
        self.assertNotEqual(id(turn[1]), id(folded[1]))

    def test_build_request_context_below_watermark(self):
        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="task"),
            AIMessage(content="", tool_calls=[{"name": "run_shell", "args": {"command": "pwd"}, "id": "c1"}]),
            ToolMessage(content="exit_code=0\n/app", tool_call_id="c1"),
        ]
        # Generous budget
        req_msgs, stats = build_request_context(messages, input_budget=10000)
        self.assertFalse(stats["compacted"])
        self.assertEqual(len(req_msgs), len(messages))
        self.assertEqual(req_msgs[3].content, "exit_code=0\n/app")

    def test_build_request_context_soft_folding_and_hard_pruning(self):
        # Construct 8 turns with large tool outputs
        messages = [
            SystemMessage(content="sys prompt"),
            HumanMessage(content="instruction"),
        ]
        for t in range(8):
            cid = f"call-{t}"
            messages.append(
                AIMessage(content="", tool_calls=[{"name": "run_shell", "args": {"command": f"cmd-{t}"}, "id": cid}])
            )
            messages.append(
                ToolMessage(content=f"exit_code=0\n" + (f"line from step {t} " * 150), tool_call_id=cid)
            )

        total_tokens = estimate_total_tokens(messages)
        self.assertGreater(total_tokens, 3000)

        # Set budget tight enough to trigger compaction
        budget = 2000
        req_msgs, stats = build_request_context(
            messages,
            input_budget=budget,
            min_recent_turns=3,
            fold_older_than_turns=3,
        )

        self.assertTrue(stats["compacted"])
        self.assertLessEqual(stats["estimated_tokens_after"], budget)
        # Prefix preserved
        self.assertIsInstance(req_msgs[0], SystemMessage)
        self.assertIsInstance(req_msgs[1], HumanMessage)
        # Must retain at least min_recent_turns (3 turns = 6 messages + 2 prefix = 8)
        self.assertGreaterEqual(len(req_msgs), 8)
        # Latest turn tool message should remain UN-folded
        last_tool = req_msgs[-1]
        self.assertIsInstance(last_tool, ToolMessage)
        self.assertIn("line from step 7", last_tool.content)

    def test_force_compact(self):
        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="task"),
            AIMessage(content="", tool_calls=[{"name": "run_shell", "args": {"command": "ls"}, "id": "c1"}]),
            ToolMessage(content="exit_code=0\n" + ("data " * 100), tool_call_id="c1"),
            AIMessage(content="", tool_calls=[{"name": "run_shell", "args": {"command": "cat"}, "id": "c2"}]),
            ToolMessage(content="exit_code=0\n" + ("data " * 100), tool_call_id="c2"),
        ]
        req_msgs, stats = build_request_context(messages, input_budget=50000, force_compact=True)
        self.assertTrue(stats["compacted"])


class FakeBox:
    def __init__(self):
        self.commands = []

    def execute(self, command, timeout):
        self.commands.append((command, timeout))
        return 0, "mock output"


class FakeLedger:
    def __init__(self):
        self.requests = 0

    def reserve(self, tokens, deadline, clock):
        self.requests += 1

    def usage(self, meta):
        pass


class TruncatedFakeModel:
    """Simulates an Ollama response that gets cut off due to context window length."""
    def __init__(self, recover_on_retry=False, prompt_eval_count=262120):
        self.calls = 0
        self.recover_on_retry = recover_on_retry
        self.prompt_eval_count = prompt_eval_count

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if not self.recover_on_retry:
            # Always cut off due to context window length
            msg = AIMessage(content="Truncated...", tool_calls=[])
            msg.response_metadata = {"done_reason": "length", "prompt_eval_count": self.prompt_eval_count, "eval_count": 10, "done": True}
            return msg
        if self.calls == 1:
            # First attempt cut off by context limit
            msg = AIMessage(content="Truncated...", tool_calls=[])
            msg.response_metadata = {"done_reason": "length", "prompt_eval_count": self.prompt_eval_count, "eval_count": 10, "done": True}
            return msg
        if self.calls == 2:
            # Recovered on emergency retry: returns a tool call
            msg = AIMessage(content="", tool_calls=[{
                "name": "run_shell", "args": {"command": "echo recovered"},
                "id": "tool-rec-1", "type": "tool_call",
            }])
            msg.response_metadata = {"done_reason": "stop", "done": True}
            return msg
        return AIMessage(content="Done", response_metadata={"done_reason": "stop"})


class OutputTruncatedFakeModel:
    """Simulates an Ollama response where output tokens hit max_output_tokens."""
    def __init__(self, recover=True, max_output_tokens=16384):
        self.calls = 0
        self.recover = recover
        self.max_output_tokens = max_output_tokens

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            # First attempt hits max_output_tokens
            msg = AIMessage(content="", tool_calls=[])
            msg.response_metadata = {
                "done_reason": "length",
                "eval_count": self.max_output_tokens,
                "prompt_eval_count": 28000,
                "done": True,
            }
            return msg
        if self.calls == 2 and self.recover:
            # Model sees warning in history and makes small targeted tool call
            msg = AIMessage(content="", tool_calls=[{
                "name": "run_shell", "args": {"command": "sed -i 's/foo/bar/' file.py"},
                "id": "tool-rec-sed", "type": "tool_call",
            }])
            msg.response_metadata = {"done_reason": "stop", "eval_count": 50, "prompt_eval_count": 28100, "done": True}
            return msg
        if not self.recover:
            # Second attempt also truncated
            msg = AIMessage(content="", tool_calls=[])
            msg.response_metadata = {
                "done_reason": "length",
                "eval_count": self.max_output_tokens,
                "prompt_eval_count": 28100,
                "done": True,
            }
            return msg
        return AIMessage(content="Done", response_metadata={"done_reason": "stop"})


class ProviderErrorFakeModel:
    """Simulates a model that throws ResponseError/500 and recovers on retry."""
    def __init__(self, recover=True):
        self.calls = 0
        self.recover = recover

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("expected element type <function> but have <parameter> (status code: 500)")
        if self.calls == 2 and self.recover:
            msg = AIMessage(content="", tool_calls=[{
                "name": "run_shell", "args": {"command": "echo recovered"},
                "id": "tool-rec-prov", "type": "tool_call",
            }])
            msg.response_metadata = {"done_reason": "stop", "eval_count": 50, "prompt_eval_count": 28100, "done": True}
            return msg
        if not self.recover:
            raise RuntimeError("expected element type <function> but have <parameter> (status code: 500)")
        return AIMessage(content="Done", response_metadata={"done_reason": "stop"})


class TestAgentContextIntegration(unittest.TestCase):
    def test_settings_input_budget_calculation(self):
        from deepswe_exp.components.configuration.settings import load_settings
        settings = load_settings(profile="saturn")
        self.assertEqual(settings.ollama_num_ctx, 262144)
        self.assertEqual(settings.ollama_context_safety_tokens, 16384)
        # input_budget = 262144 - 24576 (max_output) - 16384 (safety) = 221184
        self.assertEqual(settings.input_budget, 221184)

    def test_settings_override_study_id(self):
        from deepswe_exp.components.configuration.settings import load_settings
        settings = load_settings(profile="saturn", study_id="pilot-a-saturn-qwen36_20261120_123456")
        self.assertEqual(settings.study_id, "pilot-a-saturn-qwen36_20261120_123456")
        self.assertEqual(settings.output_root.name, "pilot-a-saturn-qwen36_20261120_123456")

    def test_classify_response_cases(self):
        from deepswe_exp.components.agent.context import classify_response

        # Case 1: Normal completion
        normal_msg = AIMessage(content="Hello", response_metadata={"done_reason": "stop"})
        res = classify_response(normal_msg, num_ctx=65536, max_output_tokens=8192, input_budget=53248)
        self.assertEqual(res["kind"], "normal")

        # Case 2: Output truncated (eval_count hit max_output_tokens, input has room)
        out_trunc_msg = AIMessage(
            content="",
            response_metadata={"done_reason": "length", "eval_count": 8192, "prompt_eval_count": 28000},
        )
        res = classify_response(out_trunc_msg, num_ctx=65536, max_output_tokens=8192, input_budget=53248)
        self.assertEqual(res["kind"], "output_truncated")

        # Case 3: Context overflow (total tokens approach num_ctx)
        ctx_overflow_msg = AIMessage(
            content="",
            response_metadata={"done_reason": "length", "eval_count": 10, "prompt_eval_count": 65500},
        )
        res = classify_response(ctx_overflow_msg, num_ctx=65536, max_output_tokens=8192, input_budget=53248)
        self.assertEqual(res["kind"], "context_overflow")

        # Case 4: Truncation unknown (done_reason=length but counters missing)
        unknown_msg = AIMessage(content="", response_metadata={"done_reason": "length"})
        res = classify_response(unknown_msg, num_ctx=65536, max_output_tokens=8192, input_budget=53248)
        self.assertEqual(res["kind"], "truncation_unknown")

    def test_run_agent_context_overflow_stop_reason(self):
        import asyncio
        from dataclasses import replace
        from pathlib import Path
        import tempfile
        from deepswe_exp.components.agent.agent import run_agent
        from deepswe_exp.components.configuration.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_settings = load_settings(profile="saturn")
            config = {**base_settings.config, "budget": {**base_settings.budget, "max_model_requests": 5}}
            settings = replace(base_settings, config=config)
            model = TruncatedFakeModel(recover_on_retry=False)
            box = FakeBox()
            ledger = FakeLedger()

            result = asyncio.run(run_agent(
                settings=settings,
                instruction="Fix the bug",
                box=box,
                output_dir=tmp_path,
                ledger=ledger,
                model=model,
            ))

            # Must stop with context_overflow, NOT model_finished!
            self.assertEqual(result["stop_reason"], "context_overflow")
            self.assertTrue((tmp_path / "errors.jsonl").is_file())
            err_content = (tmp_path / "errors.jsonl").read_text(encoding="utf-8")
            self.assertIn("ContextOverflow", err_content)

    def test_run_agent_emergency_retry_recovers(self):
        import asyncio
        from dataclasses import replace
        from pathlib import Path
        import tempfile
        from deepswe_exp.components.agent.agent import run_agent
        from deepswe_exp.components.configuration.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_settings = load_settings(profile="saturn")
            config = {**base_settings.config, "budget": {**base_settings.budget, "max_model_requests": 5}}
            settings = replace(base_settings, config=config)
            model = TruncatedFakeModel(recover_on_retry=True)
            box = FakeBox()
            ledger = FakeLedger()

            result = asyncio.run(run_agent(
                settings=settings,
                instruction="Fix the bug",
                box=box,
                output_dir=tmp_path,
                ledger=ledger,
                model=model,
            ))

            # Model recovered on retry, called tool, then finished
            self.assertEqual(result["stop_reason"], "model_finished")
            self.assertEqual(model.calls, 3)
            self.assertEqual(len(box.commands), 1)
            self.assertEqual(box.commands[0][0], "echo recovered")

    def test_run_agent_output_truncation_recovers(self):
        import asyncio
        from dataclasses import replace
        from pathlib import Path
        import tempfile
        from deepswe_exp.components.agent.agent import run_agent
        from deepswe_exp.components.configuration.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_settings = load_settings(profile="saturn")
            config = {**base_settings.config, "budget": {**base_settings.budget, "max_model_requests": 5, "max_output_tokens": 16384}}
            settings = replace(base_settings, config=config)
            model = OutputTruncatedFakeModel(recover=True)
            box = FakeBox()
            ledger = FakeLedger()

            result = asyncio.run(run_agent(
                settings=settings,
                instruction="Fix the bug",
                box=box,
                output_dir=tmp_path,
                ledger=ledger,
                model=model,
            ))

            # Turn 1: truncated. Injected warning HumanMessage.
            # Turn 2: recovered with sed command.
            # Turn 3: finished.
            self.assertEqual(result["stop_reason"], "model_finished")
            self.assertEqual(model.calls, 3)
            self.assertEqual(len(box.commands), 1)
            self.assertIn("sed -i", box.commands[0][0])
            self.assertTrue((tmp_path / "errors.jsonl").is_file())
            err_content = (tmp_path / "errors.jsonl").read_text(encoding="utf-8")
            self.assertIn("OutputTruncated", err_content)

    def test_run_agent_provider_error_recovers(self):
        import asyncio
        from dataclasses import replace
        from pathlib import Path
        import tempfile
        from deepswe_exp.components.agent.agent import run_agent
        from deepswe_exp.components.configuration.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_settings = load_settings(profile="saturn")
            config = {**base_settings.config, "budget": {**base_settings.budget, "max_model_requests": 5}}
            settings = replace(base_settings, config=config)
            model = ProviderErrorFakeModel(recover=True)
            box = FakeBox()
            ledger = FakeLedger()

            result = asyncio.run(run_agent(
                settings=settings,
                instruction="Fix the bug",
                box=box,
                output_dir=tmp_path,
                ledger=ledger,
                model=model,
            ))

            self.assertEqual(result["stop_reason"], "model_finished")
            self.assertEqual(model.calls, 3)
            self.assertEqual(len(box.commands), 1)
            self.assertEqual(box.commands[0][0], "echo recovered")
            self.assertTrue((tmp_path / "errors.jsonl").is_file())
            err_content = (tmp_path / "errors.jsonl").read_text(encoding="utf-8")
            self.assertIn("Transient provider error", err_content)

    def test_run_agent_provider_error_exhausted(self):
        import asyncio
        from dataclasses import replace
        from pathlib import Path
        import tempfile
        from deepswe_exp.components.agent.agent import run_agent
        from deepswe_exp.components.configuration.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_settings = load_settings(profile="saturn")
            config = {**base_settings.config, "budget": {**base_settings.budget, "max_model_requests": 5}}
            settings = replace(base_settings, config=config)
            model = ProviderErrorFakeModel(recover=False)
            box = FakeBox()
            ledger = FakeLedger()

            result = asyncio.run(run_agent(
                settings=settings,
                instruction="Fix the bug",
                box=box,
                output_dir=tmp_path,
                ledger=ledger,
                model=model,
            ))

            self.assertEqual(result["stop_reason"], "provider_error")
            # 1 initial call + 2 retries = 3 calls
            self.assertEqual(model.calls, 3)
            self.assertTrue((tmp_path / "errors.jsonl").is_file())
            err_content = (tmp_path / "errors.jsonl").read_text(encoding="utf-8")
            self.assertIn("expected element type", err_content)

    def test_run_agent_output_truncation_exhausted(self):
        import asyncio
        from dataclasses import replace
        from pathlib import Path
        import tempfile
        from deepswe_exp.components.agent.agent import run_agent
        from deepswe_exp.components.configuration.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_settings = load_settings(profile="saturn")
            config = {**base_settings.config, "budget": {**base_settings.budget, "max_model_requests": 5, "max_output_tokens": 16384}}
            settings = replace(base_settings, config=config)
            model = OutputTruncatedFakeModel(recover=False)
            box = FakeBox()
            ledger = FakeLedger()

            result = asyncio.run(run_agent(
                settings=settings,
                instruction="Fix the bug",
                box=box,
                output_dir=tmp_path,
                ledger=ledger,
                model=model,
            ))

            # Must stop with output_truncated after retries exhausted (1 initial + 2 retries = 3 calls)
            self.assertEqual(result["stop_reason"], "output_truncated")
            self.assertEqual(model.calls, 3)
            self.assertTrue((tmp_path / "errors.jsonl").is_file())
            err_content = (tmp_path / "errors.jsonl").read_text(encoding="utf-8")
            self.assertIn("OutputTruncated", err_content)



if __name__ == "__main__":
    unittest.main()

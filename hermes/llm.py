"""LLM backends behind one tiny interface.

OpenAIBackend talks to vLLM's OpenAI-compatible endpoint (through the SSH
tunnel, so base_url is localhost). MockBackend runs a scripted conversation
in-process — used by tests and `backend: mock` for GPU-free dry runs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string, as the OpenAI API delivers it


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMTransportError(Exception):
    pass


class OpenAIBackend:
    RETRY_DELAYS = (1, 3, 8)

    def __init__(self, cfg):
        import openai

        self._openai = openai
        self.cfg = cfg
        self.client = openai.OpenAI(
            base_url=cfg.get("base_url"),
            api_key=cfg.get("api_key", "hermes"),
            timeout=300,
        )

    def chat(self, messages, tools=None, tool_choice=None) -> ChatResult:
        sampling = self.cfg.get("sampling", {})
        kwargs = dict(
            model=self.cfg.get("model"),
            messages=messages,
            temperature=sampling.get("temperature", 0.6),
            top_p=sampling.get("top_p", 0.95),
            max_tokens=self.cfg.get("max_completion_tokens", 8192),
            extra_body={"top_k": sampling.get("top_k", 20)},
        )
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        last_error = None
        for attempt, delay in enumerate((0,) + self.RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                resp = self.client.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                calls = [
                    ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}")
                    for tc in (msg.tool_calls or [])
                ]
                return ChatResult(content=msg.content, tool_calls=calls)
            except (
                self._openai.APIConnectionError,
                self._openai.APITimeoutError,
                self._openai.InternalServerError,
            ) as e:
                last_error = e
        raise LLMTransportError(
            f"vLLM unreachable at {self.cfg.get('base_url')} after retries "
            f"({last_error}). Check `gpu status` — the tunnel may be down."
        )


class MockBackend:
    """Scripted backend. Script items:
      {"text": "..."}                       -> plain assistant message
      {"tool": "name", "args": {...}}       -> single tool call
      {"tools": [{"tool":..., "args":...}]} -> several tool calls in one turn
    When the script runs dry: echoes, and obeys forced finish_run.
    """

    def __init__(self, script: list | None = None):
        self.script = list(script or [])
        self._counter = 0

    def _tc(self, name: str, args: dict) -> ToolCall:
        self._counter += 1
        return ToolCall(f"mock-{self._counter}", name, json.dumps(args))

    def chat(self, messages, tools=None, tool_choice=None) -> ChatResult:
        if tool_choice and isinstance(tool_choice, dict):
            forced = tool_choice.get("function", {}).get("name")
            if forced == "finish_run":
                return ChatResult(
                    content=None,
                    tool_calls=[self._tc("finish_run", {"summary": "[mock] run done."})],
                )
        if self.script:
            item = self.script.pop(0)
            if "text" in item:
                return ChatResult(content=item["text"])
            if "tool" in item:
                return ChatResult(
                    content=item.get("say"),
                    tool_calls=[self._tc(item["tool"], item.get("args", {}))],
                )
            if "tools" in item:
                return ChatResult(
                    content=item.get("say"),
                    tool_calls=[
                        self._tc(t["tool"], t.get("args", {})) for t in item["tools"]
                    ],
                )
        tail = messages[-1]["content"] if messages else ""
        return ChatResult(content=f"[mock] I received: {str(tail)[-400:]}")


def make_backend(cfg):
    if cfg.get("backend") == "mock":
        return MockBackend()
    return OpenAIBackend(cfg)

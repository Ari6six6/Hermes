"""The run loop: one operator prompt -> one fresh package -> a tool-call
loop -> a final answer + a summary the next run will inherit."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from hermes import hosts as hosts_mod
from hermes import package
from hermes.llm import ChatResult, LLMTransportError
from hermes.tools import build_registry
from hermes.tools.base import ToolContext
from hermes.ui import cyan, dim, green, red, yellow

THINK_RE = re.compile(r"<(?:seed:)?think>.*?</(?:seed:)?think>\s*", re.S)
MAX_CONSECUTIVE_ERRORS = 3


@dataclass
class RunResult:
    run_id: int
    summary: str
    final_text: str
    turns: int
    aborted: bool = False


def strip_think(text: str | None) -> str:
    if not text:
        return ""
    return THINK_RE.sub("", text).strip()


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def run(project, prompt, cfg, backend, gpu=None, env=None, confirm_fn=None):
    """Execute one agent run. `env` carries gpu_status / remote_workspace /
    context_window for the package; `gpu` is an SSHEndpoint or None."""
    if confirm_fn is None:
        from hermes.confirm import confirm as confirm_fn

    env = env or {}
    host_records = hosts_mod.load_hosts()
    env.setdefault("managed_hosts", hosts_mod.hosts_env_line(host_records))
    run_id, run_dir = project.new_run()
    transcript = run_dir / "transcript.jsonl"

    def log(entry: dict):
        with transcript.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    messages = package.assemble(project, prompt, env, cfg)
    project.append_history(run_id, prompt)
    for m in messages:
        log({"role": m["role"], "content": m["content"][:200000]})

    registry = build_registry(project, cfg, confirm_fn)
    ctx = ToolContext(
        project=project,
        cfg=cfg,
        gpu=gpu,
        hosts={n: hosts_mod.host_endpoint(r) for n, r in host_records.items()},
        confirm=confirm_fn,
        served_ctx=env.get("context_window", 0),
    )
    ctx.registry = registry

    max_turns = cfg.get("max_turns", 20)
    nudges_left = cfg.get("stall_nudges", 2)
    consecutive_errors = 0
    final_text = ""
    prev_shown = ""
    turns = 0
    aborted = False
    backend_dead = False
    tool_names_used: list[str] = []

    try:
        for turns in range(1, max_turns + 1):
            result: ChatResult = backend.chat(messages, tools=registry.schemas())
            shown = strip_think(result.content)
            log(
                {
                    "role": "assistant",
                    "content": result.content,
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in result.tool_calls
                    ],
                }
            )
            repeated = bool(shown) and _normalize(shown) == _normalize(prev_shown)
            if shown:
                print(shown)
                final_text = shown
                prev_shown = shown

            if not result.tool_calls:
                # Small models love to narrate the plan (or paste code) and
                # stop instead of acting. Bounce them back a couple of times
                # before accepting prose as the final answer.
                if nudges_left <= 0:
                    break  # final answer
                nudges_left -= 1
                nudge = package.stall_nudge(repeated)
                messages.append({"role": "assistant", "content": result.content or ""})
                messages.append({"role": "user", "content": nudge})
                log({"role": "user", "content": nudge})
                print(yellow("  (model repeated itself without acting — nudging)")
                      if repeated else
                      dim("  (no tool call — nudging the model to act or finish_run)"))
                continue

            messages.append(_assistant_msg(result))
            for tc in result.tool_calls:
                if tc.name != "finish_run":
                    print(dim("  → ") + cyan(tc.name) + dim(f"({_brief(tc.arguments)})"))
                tool_names_used.append(tc.name)
                output = registry.dispatch(tc.name, tc.arguments, ctx)
                log({"role": "tool", "name": tc.name, "content": output})
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": output}
                )
                if output.startswith(("ERROR", "DENIED")):
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0

            if ctx.finish_summary is not None:
                break
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(yellow("  (circuit breaker: too many consecutive tool errors)"))
                aborted = True
                break
            if turns == max_turns - 2:
                warn = package.wrapup_warning()
                messages.append({"role": "user", "content": warn})
                log({"role": "user", "content": warn})
                print(yellow("  (2 turns left — telling the model to wrap up)"))
        else:
            print(yellow(f"  (turn cap {max_turns} reached)"))
            aborted = True
    except LLMTransportError as e:
        print(red(f"\n{e}"))
        aborted = True
        backend_dead = True
    except KeyboardInterrupt:
        print(yellow("\n(run interrupted)"))
        aborted = True
        backend_dead = True  # the operator wants out — no extra LLM round-trips

    summary = ctx.finish_summary
    if summary is None and not backend_dead:
        # Even on a cap/breaker abort the model can still write a real
        # handoff summary — far more useful to the next run than a stub.
        summary = _force_summary(backend, messages, registry, ctx, log)
    if summary is None:
        summary = _stub_summary(prompt, tool_names_used, final_text, aborted)

    (run_dir / "summary.md").write_text(summary + "\n")
    if final_text:
        (run_dir / "final.md").write_text(final_text + "\n")
    status = red("aborted") if aborted else green("complete")
    print(f"\n{dim(f'[run {run_id:04d}')} {status} {dim(f'— {turns} turn(s)]')}")
    return RunResult(run_id, summary, final_text, turns, aborted)


def _assistant_msg(result: ChatResult) -> dict:
    return {
        "role": "assistant",
        "content": result.content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in result.tool_calls
        ],
    }


def _force_summary(backend, messages, registry, ctx, log) -> str | None:
    """The model ended without finish_run — force exactly one call."""
    try:
        messages = messages + [{"role": "user", "content": package.summary_nudge()}]
        result = backend.chat(
            messages,
            tools=registry.schemas(),
            tool_choice={"type": "function", "function": {"name": "finish_run"}},
        )
        for tc in result.tool_calls:
            if tc.name == "finish_run":
                registry.dispatch(tc.name, tc.arguments, ctx)
        log({"role": "assistant", "content": "(forced finish_run)"})
        return ctx.finish_summary
    except Exception:
        return None


def _stub_summary(prompt, tools_used, final_text, aborted) -> str:
    state = "ABORTED" if aborted else "completed (no model summary)"
    return (
        f"[auto-stub, {state} {time.strftime('%Y-%m-%d %H:%M')}]\n"
        f"Prompt: {prompt[:400]}\n"
        f"Tools used: {', '.join(tools_used) if tools_used else 'none'}\n"
        f"Last output: {final_text[:400] if final_text else '(none)'}"
    )


def _brief(arguments: str, cap: int = 100) -> str:
    text = " ".join(arguments.split())
    return text[:cap] + ("…" if len(text) > cap else "")

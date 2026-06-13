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

# A fenced, multi-line code block in the final answer: ```lang\n...\n```
CODE_FENCE_RE = re.compile(r"```[^\n]*\n.*?```", re.S)

# Tools that actually create a file or execute something — i.e. that leave a
# real artifact behind. If a run produces a code block in its answer but never
# calls one of these, the "work" happened only in prose.
PRODUCTIVE_TOOLS = frozenset({
    "write_file", "edit_file",
    "remote_write", "remote_shell",
    "host_write", "host_shell",
    "local_shell", "forge_tool",
    "transfer", "replicate", "download_file",
})


def _is_phantom_finish(tool_names_used, final_text) -> bool:
    """True when the model is finishing with code in its answer but never
    wrote a file or ran anything — code that lives only in the chat reply."""
    if set(tool_names_used) & PRODUCTIVE_TOOLS:
        return False
    return bool(CODE_FENCE_RE.search(final_text or ""))


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
    phantom_nudges_left = cfg.get("phantom_nudges", 1)
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
                if tc.name != "finish_run":
                    _echo_result(output)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": output}
                )
                if output.startswith(("ERROR", "DENIED")):
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0

            if ctx.finish_summary is not None:
                if phantom_nudges_left > 0 and _is_phantom_finish(
                    tool_names_used, final_text
                ):
                    # Pasted code, wrote nothing, ran nothing — the work lives
                    # only in the reply. Reopen the run and make it real.
                    phantom_nudges_left -= 1
                    ctx.finish_summary = None
                    nudge = package.phantom_nudge()
                    messages.append({"role": "user", "content": nudge})
                    log({"role": "user", "content": nudge})
                    print(yellow("  (code in the answer but nothing written or "
                                 "run — bouncing back to actually do it)"))
                    continue
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


def _echo_result(output: str, max_lines: int = 8, cap: int = 600) -> None:
    """Show the operator the real tool result — exit codes, output, errors —
    not just the model's later prose about it. Fabricated "it passed" claims
    can't survive next to the actual output on the screen. Kept short for a
    phone: a head of lines, capped, dim (red when the tool reported trouble)."""
    text = (output or "").strip()
    if not text:
        return
    all_lines = text.splitlines()
    lines = all_lines[:max_lines]
    shown = "\n".join(lines)
    if len(shown) > cap:
        shown = shown[:cap] + " …"
        lines = shown.splitlines()
    color = red if text.startswith(("ERROR", "DENIED")) else dim
    for line in lines:
        print(color("    " + line))
    extra = len(all_lines) - len(lines)
    if extra > 0:
        print(dim(f"    … (+{extra} more line(s))"))

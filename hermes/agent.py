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
from hermes.ui import cyan, dim, green, magenta, red, yellow

THINK_RE = re.compile(r"<(?:seed:)?think>.*?</(?:seed:)?think>\s*", re.S)
VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL)", re.I)
MAX_CONSECUTIVE_ERRORS = 3

# Tools that put code on disk — the trigger for an independent verification
# pass. (Running-only tasks like "check the logs" don't need code-verifying.)
CODE_WRITE_TOOLS = frozenset({"write_file", "edit_file", "remote_write"})

# In build mode, checking your work against the twin means actually exercising it —
# replaying its ground-truth response (twin_request) or re-checking a request
# against the live target (twin_reground). Finishing a code change without ever
# doing this is the "told my guy it worked and pissed off" move — the one thing
# the build is built to prevent.
BUILD_PROOF_TOOLS = frozenset({"twin_request", "twin_reground"})

# What counts as the antithesis having REALLY exercised something — running the
# solution or querying the twin. A passive read (read_file, remote_read,
# twin_map, ...) is not evidence: in build mode a VERDICT: PASS backed only by a
# read is collusion theater (the critic just eyeballed the code and agreed).
VERIFY_EVIDENCE_TOOLS = frozenset({
    "remote_shell", "local_shell", "host_shell", "build_run", "twin_request",
})

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


def strip_think(text: str | None, pattern: "re.Pattern" = THINK_RE) -> str:
    if not text:
        return ""
    return pattern.sub("", text).strip()


def _think_re(tags) -> "re.Pattern":
    """Build the reasoning-stripper for a model's own tags. Hermes emits
    <think>/<seed:think>; Qwen uses <think>; some finetunes add <thinking>."""
    alt = "|".join(re.escape(t) for t in tags) or "think"
    return re.compile(rf"<(?:{alt})>.*?</(?:{alt})>\s*", re.S)


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def run(project, prompt, cfg, backend, gpu=None, env=None, confirm_fn=None,
        sandbox=None):
    """Execute one agent run. `env` carries gpu_status / remote_workspace /
    context_window for the package; `gpu` is an SSHEndpoint or None; `sandbox` is
    the VPS sandbox-host SSHEndpoint (where the runtime twin lives) or None."""
    if confirm_fn is None:
        from hermes.confirm import confirm as confirm_fn

    env = env or {}
    from hermes.models import resolve as resolve_model

    spec = resolve_model(cfg)
    think_re = _think_re(spec.think_tags)
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
        sandbox=sandbox,
        hosts={n: hosts_mod.host_endpoint(r) for n, r in host_records.items()},
        confirm=confirm_fn,
        served_ctx=env.get("context_window", 0),
    )
    ctx.registry = registry

    max_turns = cfg.get("max_turns", 20)
    nudges_left = cfg.get("stall_nudges", 2)
    phantom_nudges_left = cfg.get("phantom_nudges", 1)
    # Build mode = this project has a sealed twin to prove work against.
    try:
        twin_sealed = project.twin().is_sealed()
    except Exception:
        twin_sealed = False
    build_proof_nudges_left = cfg.get("build_proof_nudges", 1) if twin_sealed else 0
    # Independent verification only runs when there's a real sandbox to run the
    # code in (a GPU box) and the operator hasn't switched it off.
    verify_rounds_left = (
        cfg.get("verify_rounds", 2)
        if cfg.get("verify_code_runs", True) and gpu is not None
        else 0
    )
    consecutive_errors = 0
    final_text = ""
    prev_shown = ""
    turns = 0
    aborted = False
    backend_dead = False
    tool_names_used: list[str] = []
    files_touched: list[str] = []

    # Planner (build mode): before any code is written, an independent pass lays
    # out an ordered checklist the builder executes against and the antithesis
    # checks. On by default for sealed-twin tasks; off via `plan_build_tasks`.
    if twin_sealed and cfg.get("plan_build_tasks", True):
        print(magenta("  (planner — laying out the checklist before building)"))
        plan = _plan(backend, prompt, project, think_re)
        if plan:
            messages.append({"role": "user", "content": package.plan_brief(plan)})
            log({"role": "planner", "content": plan})

    try:
        for turns in range(1, max_turns + 1):
            result: ChatResult = backend.chat(messages, tools=registry.schemas())
            shown = strip_think(result.content, think_re)
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
                    if tc.name in CODE_WRITE_TOOLS:
                        path = _arg(tc.arguments, "path")
                        if path and path not in files_touched:
                            files_touched.append(path)

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
                if (
                    build_proof_nudges_left > 0
                    and (set(tool_names_used) & CODE_WRITE_TOOLS)
                    and not (set(tool_names_used) & BUILD_PROOF_TOOLS)
                ):
                    # Build mode: changed code but never checked it against the
                    # twin. That's the "tell my guy it worked and piss off" move.
                    # Send it back to prove parity with a real query, not a claim.
                    build_proof_nudges_left -= 1
                    ctx.finish_summary = None
                    nudge = package.build_proof_nudge()
                    messages.append({"role": "user", "content": nudge})
                    log({"role": "user", "content": nudge})
                    print(red("  (build: code changed but never run against the "
                              "twin — sending it back to PROVE it, not claim it)"))
                    continue
                if verify_rounds_left > 0 and (
                    set(tool_names_used) & CODE_WRITE_TOOLS
                ):
                    # The doer doesn't get to grade its own homework. A fresh,
                    # skeptical pass re-runs the code in the real sandbox and
                    # returns a verdict the doer can't fake.
                    verify_rounds_left -= 1
                    print(magenta(
                        "  (antithesis — breaking the solution against the twin)"
                        if twin_sealed else
                        "  (independent verification — re-running the code in the sandbox)"))
                    passed, report = _verify(
                        backend, registry, ctx, prompt, files_touched, log,
                        cfg.get("verify_max_turns", 6), build=twin_sealed,
                        think_re=think_re,
                    )
                    if not passed:
                        if (twin_sealed and verify_rounds_left == 0
                                and cfg.get("referee_on_deadlock", True)):
                            # Deadlock: out of verify rounds and the antithesis is
                            # still failing the solution the doer keeps finishing.
                            # The referee makes the binding call with fresh eyes,
                            # instead of silently accepting an unverified finish.
                            print(magenta("  (referee — builder and antithesis "
                                          "deadlocked; making the final call)"))
                            ref_passed, ref_report = _referee(
                                backend, registry, ctx, prompt, files_touched,
                                report, log, cfg.get("verify_max_turns", 6),
                                think_re=think_re,
                            )
                            if ref_passed:
                                print(green("  (referee ruled the solution holds "
                                            "— accepting)"))
                                break
                            ctx.finish_summary = None
                            nudge = package.referee_failed(ref_report)
                            messages.append({"role": "user", "content": nudge})
                            log({"role": "user", "content": nudge})
                            print(red("  (referee upheld the failure — back to fix it)"))
                            continue
                        ctx.finish_summary = None
                        nudge = package.verify_failed(report)
                        messages.append({"role": "user", "content": nudge})
                        log({"role": "user", "content": nudge})
                        print(red("  (antithesis BROKE it — sending it back to fix "
                                  "the real problem)" if twin_sealed else
                                  "  (verification FAILED — sending it back to fix "
                                  "the real problem)"))
                        continue
                    print(green("  (antithesis could not break it — it holds against "
                                "the twin)" if twin_sealed else
                                "  (verification PASSED — the code actually runs)"))
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
    # `not summary` (not `is None`) so a finish_run whose summary stripped to ""
    # still falls through to a real handoff instead of writing an empty one.
    if not summary and not backend_dead:
        # Even on a cap/breaker abort the model can still write a real
        # handoff summary — far more useful to the next run than a stub.
        summary = _force_summary(
            backend, messages, registry, ctx, log,
            force=spec.supports_forced_tool_choice,
        )
    if not summary:
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


def _force_summary(backend, messages, registry, ctx, log, force=True) -> str | None:
    """The model ended without finish_run — ask for exactly one call. On vLLM
    we pin tool_choice to finish_run; on runtimes that don't honour named
    tool_choice (llama.cpp under --jinja) we send the nudge plain and accept a
    finish_run if the model offers one, else fall back to a stub upstream."""
    try:
        messages = messages + [{"role": "user", "content": package.summary_nudge()}]
        kwargs = {"tools": registry.schemas()}
        if force:
            kwargs["tool_choice"] = {"type": "function", "function": {"name": "finish_run"}}
        result = backend.chat(messages, **kwargs)
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


def _arg(arguments: str, key: str):
    try:
        value = json.loads(arguments or "{}").get(key)
    except (json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def _plan(backend, request, project, think_re=THINK_RE) -> str:
    """A pre-thesis pass: turn the mission + request into an ordered checklist the
    builder executes against. No tools — it only thinks and writes the plan.
    Returns "" on any failure so a missing plan never blocks the run."""
    try:
        result = backend.chat([
            {"role": "system", "content": package.planner_prompt()},
            {"role": "user", "content": package.planner_request(project, request)},
        ])
    except LLMTransportError:
        return ""
    return strip_think(result.content, think_re)


def _critic_pass(backend, registry, ctx, system, user, label, log, max_turns,
                 require_evidence, no_evidence_msg, think_re=THINK_RE) -> tuple[bool, str]:
    """One independent reviewing pass: fresh context, a skeptical prompt, the
    same real sandbox. Re-runs the code itself and returns (passed, report).
    Fails closed — no clear PASS verdict means FAIL. When `require_evidence` is
    set, a PASS is rejected unless the pass actually ran/queried something real
    (`VERIFY_EVIDENCE_TOOLS`), because author and critic share the same weights."""
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    report = ""
    executed = False  # did the critic run/query anything that returned real output?
    for _ in range(max(1, max_turns)):
        try:
            result = backend.chat(msgs, tools=registry.schemas())
        except LLMTransportError:
            return False, f"(the {label} could not reach the backend)"
        shown = strip_think(result.content, think_re)
        log({
            "role": label,
            "content": result.content,
            "tool_calls": [{"name": tc.name, "arguments": tc.arguments}
                           for tc in result.tool_calls],
        })
        if shown:
            report = shown
            print(magenta(f"  [{label}] ") + dim(_brief(shown.splitlines()[0], 120)))
        verdicts = VERDICT_RE.findall(shown) if shown else []
        if verdicts:
            passed = verdicts[-1].upper() == "PASS"
            if require_evidence and passed and not executed:
                return False, no_evidence_msg
            return passed, report
        if not result.tool_calls:
            break  # ended without a verdict and without acting — inconclusive
        msgs.append(_assistant_msg(result))
        for tc in result.tool_calls:
            if tc.name == "finish_run":
                out = (f"Not your tool — you are the {label}. Run the code and "
                       "end with a line 'VERDICT: PASS' or 'VERDICT: FAIL'.")
            else:
                out = registry.dispatch(tc.name, tc.arguments, ctx)
                if tc.name in VERIFY_EVIDENCE_TOOLS and not out.startswith(
                    ("ERROR", "DENIED")
                ):
                    executed = True
                print(dim(f"    [{label}] → ") + cyan(tc.name))
                _echo_result(out)
            log({"role": f"{label}-tool", "name": tc.name, "content": out})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    return False, report or f"(the {label} produced no verdict)"


def _verify(backend, registry, ctx, request, files, log, max_turns,
            build=False, think_re=THINK_RE) -> tuple[bool, str]:
    """The doer doesn't grade its own homework. In build mode this is the
    ANTITHESIS (diff the solution against the twin, anti-collusion evidence
    required); otherwise the plain verifier (re-run the code, text PASS ok)."""
    if build:
        return _critic_pass(
            backend, registry, ctx,
            package.antithesis_prompt(),
            package.antithesis_request(ctx.project, request, files),
            "antithesis", log, max_turns, require_evidence=True,
            no_evidence_msg=(
                "VERDICT PASS rejected — the antithesis never ran the solution or "
                "the twin, so it has no evidence the outputs actually match. "
                "Treating as FAIL."),
            think_re=think_re,
        )
    return _critic_pass(
        backend, registry, ctx,
        package.verifier_prompt(),
        package.verifier_request(request, files),
        "verifier", log, max_turns, require_evidence=False,
        no_evidence_msg="", think_re=think_re,
    )


def _referee(backend, registry, ctx, request, files, antithesis_report, log,
             max_turns, think_re=THINK_RE) -> tuple[bool, str]:
    """The tie-breaker, invoked only on deadlock (verify rounds spent, antithesis
    still failing). Fresh eyes, the real sandbox, and the authority to overrule
    either side — but a PASS needs real executed evidence or the antithesis stands."""
    return _critic_pass(
        backend, registry, ctx,
        package.referee_prompt(),
        package.referee_request(request, files, antithesis_report),
        "referee", log, max_turns, require_evidence=True,
        no_evidence_msg=(
            "VERDICT PASS rejected — the referee ran nothing, so it has no "
            "evidence to overturn the antithesis. The antithesis stands; FAIL."),
        think_re=think_re,
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

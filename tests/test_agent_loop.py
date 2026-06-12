import json

from hermes import agent
from hermes.llm import MockBackend


def run_agent(project, cfg, script, confirm=None):
    backend = MockBackend(script)
    return agent.run(
        project,
        "do the thing",
        cfg,
        backend,
        gpu=None,
        env={},
        confirm_fn=confirm or (lambda *a, **k: True),
    )


def test_happy_path_with_finish_run(project, cfg):
    result = run_agent(
        project,
        cfg,
        [
            {"tool": "write_file",
             "args": {"path": "workspace/out.txt", "content": "hello"}},
            {"tool": "finish_run", "args": {"summary": "Did: wrote out.txt"}},
        ],
    )
    assert not result.aborted
    assert result.summary == "Did: wrote out.txt"
    assert (project.workspace_dir / "out.txt").read_text() == "hello"
    assert (project.runs_dir / "0001" / "summary.md").read_text().strip() == \
        "Did: wrote out.txt"
    assert (project.runs_dir / "0001" / "transcript.jsonl").exists()
    # prompt landed in history
    assert project.recent_prompts(5)[-1]["text"] == "do the thing"


def test_forced_summary_when_model_forgets(project, cfg):
    cfg.set("stall_nudges", 0)  # legacy path: prose is accepted as final immediately
    result = run_agent(project, cfg, [{"text": "all done, bye"}])
    assert not result.aborted
    assert result.summary == "[mock] run done."  # MockBackend obeys forced finish_run
    assert result.final_text == "all done, bye"


def test_stall_nudge_gets_model_to_act(project, cfg):
    result = run_agent(
        project,
        cfg,
        [
            {"text": "I should write out.txt with hello."},  # narrates, no tool call
            {"tool": "write_file",
             "args": {"path": "workspace/out.txt", "content": "hello"}},
            {"tool": "finish_run", "args": {"summary": "Did: wrote out.txt"}},
        ],
    )
    assert not result.aborted
    assert result.summary == "Did: wrote out.txt"
    assert (project.workspace_dir / "out.txt").read_text() == "hello"
    transcript = (project.runs_dir / "0001" / "transcript.jsonl").read_text()
    assert "prose and no tool call" in transcript  # the nudge landed


def test_stall_nudge_flags_repetition(project, cfg):
    result = run_agent(
        project,
        cfg,
        [
            {"text": "I should write the file."},
            {"text": "I should write   the file."},  # same thing, modulo whitespace
            {"tool": "finish_run", "args": {"summary": "done"}},
        ],
    )
    assert not result.aborted
    transcript = (project.runs_dir / "0001" / "transcript.jsonl").read_text()
    assert "same message twice" in transcript


def test_stall_nudges_exhausted_accepts_prose(project, cfg):
    result = run_agent(
        project,
        cfg,
        [{"text": "thinking..."}, {"text": "still thinking..."}, {"text": "the answer"}],
    )
    assert not result.aborted
    assert result.final_text == "the answer"  # third prose turn accepted as final
    assert result.summary == "[mock] run done."  # forced finish_run backstop


def test_turn_cap_stub_summary(project, cfg):
    cfg.set("max_turns", 2)
    script = [{"tool": "write_note", "args": {"text": f"n{i}"}} for i in range(5)]
    result = run_agent(project, cfg, script)
    assert result.aborted
    assert result.turns == 2
    assert "[auto-stub" in result.summary


def test_circuit_breaker_on_consecutive_errors(project, cfg):
    script = [
        {"tool": "read_file", "args": {"path": "workspace/missing.txt"}}
        for _ in range(5)
    ]
    result = run_agent(project, cfg, script)
    assert result.aborted
    assert result.turns == 3  # breaker trips after 3 consecutive ERROR results


def test_denied_local_shell_feeds_back(project, cfg):
    script = [
        {"tool": "local_shell", "args": {"command": "rm -rf /"}},
        {"tool": "finish_run", "args": {"summary": "operator said no"}},
    ]
    result = run_agent(project, cfg, script, confirm=lambda *a, **k: False)
    assert result.summary == "operator said no"
    transcript = (project.runs_dir / "0001" / "transcript.jsonl").read_text()
    assert "DENIED by operator" in transcript


def test_remote_tools_without_gpu(project, cfg):
    script = [
        {"tool": "remote_shell", "args": {"command": "ls"}},
        {"tool": "finish_run", "args": {"summary": "no gpu"}},
    ]
    result = run_agent(project, cfg, script)
    transcript = (project.runs_dir / "0001" / "transcript.jsonl").read_text()
    assert "no GPU box attached" in transcript
    assert result.summary == "no gpu"


def test_think_blocks_stripped():
    assert agent.strip_think("<think>secret</think>answer") == "answer"
    assert agent.strip_think("<seed:think>x</seed:think>ok") == "ok"
    assert agent.strip_think(None) == ""

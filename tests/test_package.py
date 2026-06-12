from hermes import package


def test_sections_and_order(project, cfg):
    project.append_history(1, "first prompt")
    run_id, run_dir = project.new_run()
    (run_dir / "summary.md").write_text("did a thing")
    project.append_note("remember the thing")
    (project.workspace_dir / "f.txt").write_text("x")

    messages = package.assemble(project, "what now?", {}, cfg)
    assert [m["role"] for m in messages] == ["system", "user"]
    user = messages[1]["content"]
    order = [
        user.index("# MISSION"),
        user.index("# PROMPT HISTORY"),
        user.index("# RUN SUMMARIES"),
        user.index("# YOUR LAST REPLY"),
        user.index("# NOTES"),
        user.index("# WORKSPACE"),
        user.index("# CURRENT REQUEST"),
    ]
    assert order == sorted(order)
    assert "first prompt" in user
    assert "did a thing" in user
    assert "remember the thing" in user
    assert "what now?" in user
    assert "f.txt" in user


def test_empty_project(project, cfg):
    messages = package.assemble(project, "hello", {}, cfg)
    user = messages[1]["content"]
    assert "(none yet)" in user
    assert "hello" in user


def test_notes_truncation_keeps_tail(project, cfg):
    for i in range(3000):
        project.append_note(f"note number {i}")
    messages = package.assemble(project, "go", {}, cfg)
    user = messages[1]["content"]
    assert "[...truncated...]" in user
    assert "note number 2999" in user
    assert "note number 0\n" not in user


def test_budget_scales_with_context_window(cfg):
    big = package.package_budget_chars(cfg, 0)
    small = package.package_budget_chars(cfg, 16384)
    assert small < big
    assert small == int(16384 * 0.30) * package.APPROX_CHARS_PER_TOKEN


def test_system_prompt_renders(project, cfg):
    messages = package.assemble(
        project, "x", {"gpu_status": "h:1 (vllm:up)", "context_window": 131072}, cfg
    )
    system = messages[0]["content"]
    assert "{{" not in system  # all template vars replaced
    assert "testproj" in system
    assert "131072" in system
    assert "internet access goes through the phone" in system.lower()
    assert "Managed hosts: none" in system  # default when none registered


def test_system_prompt_lists_managed_hosts(project, cfg):
    env = {"managed_hosts": "web=root@1.2.3.4:22 (primary web)"}
    messages = package.assemble(project, "x", env, cfg)
    assert "web=root@1.2.3.4:22" in messages[0]["content"]


def test_config_saved_private(cfg):
    import stat

    from hermes.config import config_path

    cfg.save()
    assert stat.S_IMODE(config_path().stat().st_mode) == 0o600

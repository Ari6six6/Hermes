from hermes import package
from hermes.twin.model import Exchange


def test_init_repo_mode_stores_mode_and_ref(project):
    twin = project.twin()
    twin.init(source="https://github.com/acme/widget", mode="repo", ref="v2.1.0")
    assert twin.mode == "repo"
    assert twin.ref == "v2.1.0"
    assert "[repo]" in twin.summary()


def test_recon_build_block_uses_repo_prompt_for_repo_mode(project):
    twin = project.twin()
    twin.init(source="https://github.com/acme/widget", mode="repo", ref="main")
    twin.add_exchange(Exchange(method="GET", path="/", status=200, response_body="x"))
    block = package.recon_build_block(project)
    assert "stand up the twin from a repo" in block
    assert "github.com/acme/widget" in block
    assert "pin to `main`" in block          # ref surfaced
    assert "be* the real software" in block  # the high-fidelity framing


def test_recon_build_block_uses_url_prompt_for_url_mode(project):
    twin = project.twin()
    twin.init(source="https://api.example.com", mode="url")
    twin.add_exchange(Exchange(method="GET", path="/", status=200, response_body="x"))
    block = package.recon_build_block(project)
    assert "stand up the twin" in block
    assert "from a repo" not in block        # the URL prompt, not the repo one


def test_repo_twin_seals_like_any_other(project):
    # Repo and URL twins converge on the same sealed representation, so the rest
    # of the pipeline (build mode, antithesis, serve) is mode-agnostic.
    twin = project.twin()
    twin.init(source="https://github.com/acme/widget", mode="repo")
    twin.add_exchange(Exchange(method="GET", path="/ping", status=200, response_body="pong"))
    twin.seal()
    assert twin.is_sealed()
    assert package.recon_build_block(project) == ""           # left recon phase
    # build-mode framing applies regardless of how the twin was sourced
    assert "SAFE TWIN" in package.build_mode_block(project)

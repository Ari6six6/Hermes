from pathlib import Path

import pytest

from hermes.config import Config
from hermes.project import Project


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def cfg(home, tmp_path):
    c = Config.load()
    c.set("projects_dir", str(tmp_path / "projects"))
    return c


@pytest.fixture
def project(cfg):
    return Project.create(Path(cfg.get("projects_dir")), "testproj")


@pytest.fixture
def yes(monkeypatch):
    """Confirmation function that always approves."""
    return lambda *a, **k: True


@pytest.fixture
def no():
    return lambda *a, **k: False

"""Self-update guard tests. The critical property is the anti-restart-loop no-op
guard: when HEAD == origin/<branch>, check_and_update must NOT request a restart.
"""
import logging
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from syncronizer import updater

LOG = logging.getLogger("test")
HAS_GIT = shutil.which("git") is not None


def _paths(tmp_path, repo_dir):
    return SimpleNamespace(
        repo_dir=repo_dir,
        venv_dir=None,
        git_exe=None,
        last_applied_commit=tmp_path / "state" / "last_applied_commit",
        quarantine=tmp_path / "state" / "quarantine.json",
    )


def _app(tmp_path, repo_dir, auto_update=True):
    flag = {"restart": False}
    settings = SimpleNamespace(
        auto_update=auto_update, update_branch="main", allow_hard_reset=False
    )
    app = SimpleNamespace(
        settings=settings,
        paths=_paths(tmp_path, repo_dir),
        request_restart=lambda: flag.__setitem__("restart", True),
        _flag=flag,
    )
    return app


def test_skip_when_auto_update_off(tmp_path):
    app = _app(tmp_path, tmp_path / "repo", auto_update=False)
    assert updater.check_and_update(app, LOG) is False


def test_skip_when_no_repo(tmp_path):
    app = _app(tmp_path, tmp_path / "norepo")
    assert updater.check_and_update(app, LOG) is False


def test_write_marker_atomic(tmp_path):
    marker = tmp_path / "state" / "marker"
    updater.write_marker(marker, "abc123")
    assert marker.read_text() == "abc123"


def test_current_sha_none_without_repo(tmp_path):
    paths = _paths(tmp_path, tmp_path / "norepo")
    assert updater.current_sha(paths) is None


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_noop_when_up_to_date(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "requirements.txt").write_text("x\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "origin", "main")

    repo = tmp_path / "repo"
    _git(tmp_path, "clone", str(remote), str(repo))

    app = _app(tmp_path, repo)
    assert updater.check_and_update(app, LOG) is False
    assert app._flag["restart"] is False

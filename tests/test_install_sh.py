"""Tests for install.sh upstream URL resolution logic."""
import os
import subprocess


def _run_resolution(tmp_path, *, source_mode, repo_url="https://github.com/nj4x/anthrouter.git", script_dir=""):
    script = tmp_path / "_resolve.sh"
    script.write_text(
        r"""#!/usr/bin/env bash
set -euo pipefail
die() { echo "error: $*" >&2; exit 1; }
if [ "$SOURCE_MODE" = "clone" ]; then
    UPSTREAM_URL="$REPO_URL"
else
    UPSTREAM_URL="$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null)" \
        || die "Cannot determine upstream URL: '$SCRIPT_DIR' has no 'origin' remote. Add an origin remote and re-run install.sh."
fi
echo "$UPSTREAM_URL"
"""
    )
    script.chmod(0o755)
    env = {
        **os.environ,
        "SOURCE_MODE": source_mode,
        "REPO_URL": repo_url,
        "SCRIPT_DIR": script_dir,
    }
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, check=False)


def test_clone_mode_upstream_url(tmp_path):
    result = _run_resolution(
        tmp_path,
        source_mode="clone",
        repo_url="https://github.com/nj4x/anthrouter.git",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "https://github.com/nj4x/anthrouter.git"


def test_local_mode_upstream_url_from_origin(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    origin_url = "https://github.com/fork/anthrouter.git"
    subprocess.run(
        ["git", "remote", "add", "origin", origin_url],
        cwd=repo, check=True, capture_output=True,
    )
    result = _run_resolution(tmp_path, source_mode="local", script_dir=str(repo))
    assert result.returncode == 0
    assert result.stdout.strip() == origin_url


def test_local_mode_no_origin_aborts(tmp_path):
    repo = tmp_path / "norepo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    result = _run_resolution(tmp_path, source_mode="local", script_dir=str(repo))
    assert result.returncode != 0
    assert "has no 'origin' remote" in result.stderr
    assert "re-run install.sh" in result.stderr

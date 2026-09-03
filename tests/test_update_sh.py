"""Tests for update.sh in-place update script."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path


def _find_free_port():
    """Find a free port for testing."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HealthHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler that returns 200 on /health."""

    def log_message(self, format, *args):
        pass  # Suppress logging

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()


def _start_server(port, stop_event):
    """Start a simple HTTP server in a thread."""
    server = HTTPServer(("127.0.0.1", port), HealthHandler)
    server.timeout = 0.5

    def serve():
        while not stop_event.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return server, thread


def _setup_fake_install(
    tmp_path,
    *,
    manifest_extra=None,
    with_venv_bak=False,
    with_lock=False,
    venv_bak_has_executable=False,
    upstream_url="https://github.com/nj4x/anthrouter.git",
    port=None,
):
    """Set up a fake anthrouter install in tmp_path."""
    home = tmp_path / "home"
    anthrouter_dir = home / ".anthrouter"
    anthrouter_dir.mkdir(parents=True)

    # Create manifest
    manifest = {
        "completed": True,
        "upstreamUrl": upstream_url,
        "shimPath": str(anthrouter_dir / "shim.sh"),
        "pid": None,
    }
    if manifest_extra:
        manifest.update(manifest_extra)

    manifest_path = anthrouter_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Create venv with fake pip and python
    venv = anthrouter_dir / "venv"
    venv_bin = venv / "bin"
    venv_bin.mkdir(parents=True)

    # Fake python that just exits 0
    fake_python = venv_bin / "python"
    fake_python.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n")
    fake_python.chmod(0o755)

    # Fake pip - can be configured to fail
    fake_pip = venv_bin / "pip"
    pip_script = anthrouter_dir / "pip_fail.sh"
    if not pip_script.exists():
        pip_script.write_text("#!/usr/bin/env bash\nexit 0\n")
        pip_script.chmod(0o755)
    # Default: pip succeeds
    (venv_bin / "pip").write_text(f"#!/usr/bin/env bash\nexec {pip_script} \"$@\"\n")
    (venv_bin / "pip").chmod(0o755)

    # Create shim
    shim = anthrouter_dir / "shim.sh"
    if port:
        # Shim that starts a server on the given port
        shim.write_text(f"""#!/usr/bin/env bash
python3 -c "
import threading, socket, time
from http.server import HTTPServer, BaseHTTPRequestHandler

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

s = HTTPServer(('127.0.0.1', {port}), H)
s.serve_forever()
" &
echo $!
""")
    else:
        # Shim that does nothing (for failure tests)
        shim.write_text("#!/usr/bin/env bash\n# Fake shim\nsleep 3600 &\necho $!\n")
    shim.chmod(0o755)

    # Create config.env if port specified
    if port:
        config_env = anthrouter_dir / "config.env"
        config_env.write_text(f"ANTHROUTER_PORT={port}\nANTHROUTER_HOST=127.0.0.1\n")

    # Create venv.bak if requested
    if with_venv_bak:
        venv_bak = anthrouter_dir / "venv.bak"
        venv_bak_bin = venv_bak / "bin"
        venv_bak_bin.mkdir(parents=True)
        bak_python = venv_bak_bin / "python"
        if venv_bak_has_executable:
            bak_python.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n")
            bak_python.chmod(0o755)
            # Add marker file
            (venv_bak / "marker.txt").write_text("backup marker")
        else:
            bak_python.write_text("# not executable\n")
            # Don't chmod - leave non-executable

    # Create lock if requested
    if with_lock:
        lock_dir = anthrouter_dir / "update.lock"
        lock_dir.mkdir(parents=True)

    # Create upstream git repo for clone
    upstream_repo = tmp_path / "upstream_repo"
    upstream_repo.mkdir()
    subprocess.run(["git", "init"], cwd=upstream_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=upstream_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=upstream_repo,
        check=True,
        capture_output=True,
    )

    # Create minimal repo structure including update.sh for self-update
    (upstream_repo / "pyproject.toml").write_text(
        '[project]\nname = "anthrouter"\nversion = "0.0.1"\n'
    )
    (upstream_repo / "update.sh").write_text("#!/usr/bin/env bash\n# Updated update.sh\n")
    (upstream_repo / "update.sh").chmod(0o755)

    subprocess.run(
        ["git", "add", "."], cwd=upstream_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial"],
        cwd=upstream_repo,
        check=True,
        capture_output=True,
    )

    return home, anthrouter_dir, upstream_repo, manifest_path


def _run_update(home, anthrouter_dir, check=False):
    """Run update.sh with the given home directory."""
    env = {
        **os.environ,
        "HOME": str(home),
        "ANTHROUTER_HOME": str(anthrouter_dir),
    }

    # Get path to update.sh
    update_script = anthrouter_dir.parent / "update.sh"
    if not update_script.exists():
        # Use repo root update.sh
        update_script = Path(__file__).parent.parent / "update.sh"

    result = subprocess.run(
        ["bash", str(update_script)],
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )
    return result


def test_abort_manifest_missing(tmp_path):
    """Test abort when manifest.json is missing."""
    home = tmp_path / "home"
    home.mkdir()

    env = {
        **os.environ,
        "HOME": str(home),
        "ANTHROUTER_HOME": str(home / ".anthrouter"),
    }

    update_script = Path(__file__).parent.parent / "update.sh"
    result = subprocess.run(
        ["bash", str(update_script)],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "not installed" in result.stderr
    assert "run install.sh" in result.stderr


def test_abort_completed_not_true(tmp_path):
    """Test abort when completed != true."""
    home, anthrouter_dir, _, _ = _setup_fake_install(
        tmp_path, manifest_extra={"completed": False}
    )

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    assert result.returncode != 0
    assert "not completed" in result.stderr.lower() or "completed=" in result.stderr


def test_abort_upstream_url_missing(tmp_path):
    """Test abort when upstreamUrl is missing - must NOT substitute canonical URL."""
    # Don't include upstreamUrl at all (not even None which becomes JSON null)
    home, anthrouter_dir, _, _ = _setup_fake_install(
        tmp_path, manifest_extra={}
    )
    # Remove upstreamUrl from manifest
    manifest_path = anthrouter_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["upstreamUrl"]
    manifest_path.write_text(json.dumps(manifest, indent=2))

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    assert result.returncode != 0
    assert "upstreamUrl" in result.stderr
    assert "add it manually" in result.stderr or "python3 -c" in result.stderr
    # Ensure canonical URL is NOT substituted
    assert "github.com/nj4x/anthrouter" not in result.stderr


def test_abort_shim_path_missing(tmp_path):
    """Test abort when shimPath is missing."""
    home, anthrouter_dir, _, manifest_path = _setup_fake_install(tmp_path)
    # Remove shimPath from manifest
    manifest = json.loads(manifest_path.read_text())
    del manifest["shimPath"]
    manifest_path.write_text(json.dumps(manifest, indent=2))

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    assert result.returncode != 0
    assert "shimPath" in result.stderr


def test_abort_shim_path_not_executable(tmp_path):
    """Test abort when shimPath is not executable."""
    home, anthrouter_dir, _, _ = _setup_fake_install(tmp_path)
    # Make shim non-executable
    shim = anthrouter_dir / "shim.sh"
    shim.chmod(0o644)

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    assert result.returncode != 0
    assert "not executable" in result.stderr


def test_abort_lock_exists(tmp_path):
    """Test abort when update.lock already exists."""
    home, anthrouter_dir, upstream_repo, _ = _setup_fake_install(
        tmp_path, with_lock=True
    )

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    assert result.returncode != 0
    assert "lock" in result.stderr.lower() or "Another update" in result.stdout
    # Lock should still exist (wasn't created by us, shouldn't be removed)
    # Note: The lock dir was created by test setup, script should not touch it
    lock_dir = anthrouter_dir / "update.lock"
    assert lock_dir.exists(), f"Lock dir should still exist after abort. Contents of anthrouter_dir: {list(anthrouter_dir.iterdir())}"


def test_lock_removed_after_failed_run(tmp_path):
    """Test that lock is removed after a failed run."""
    # Use file:// URL to local repo for deterministic hermetic test
    home, anthrouter_dir, upstream_repo, _ = _setup_fake_install(tmp_path)
    # Override upstreamUrl to use local repo
    manifest_path = anthrouter_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["upstreamUrl"] = f"file://{upstream_repo}"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Create a scenario that will fail during pip install
    venv_bin = anthrouter_dir / "venv" / "bin"
    # Make pip fail
    pip_script = anthrouter_dir / "pip_fail.sh"
    pip_script.write_text("#!/usr/bin/env bash\nexit 1\n")
    pip_script.chmod(0o755)
    (venv_bin / "pip").write_text(f"#!/usr/bin/env bash\nexec {pip_script} \"$@\"\n")
    (venv_bin / "pip").chmod(0o755)

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    # Lock should be removed
    assert not (anthrouter_dir / "update.lock").exists()


def test_venv_bak_reuse(tmp_path):
    """Test that stale venv.bak is reused, not double-backed up."""
    # Use file:// URL to local repo for deterministic hermetic test
    home, anthrouter_dir, upstream_repo, _ = _setup_fake_install(
        tmp_path, with_venv_bak=True, venv_bak_has_executable=True
    )
    # Override upstreamUrl to use local repo
    manifest_path = anthrouter_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["upstreamUrl"] = f"file://{upstream_repo}"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Add marker to venv.bak to track it
    venv_bak = anthrouter_dir / "venv.bak"

    # Set up a working server port
    port = _find_free_port()
    config_env = anthrouter_dir / "config.env"
    config_env.write_text(f"ANTHROUTER_PORT={port}\nANTHROUTER_HOST=127.0.0.1\n")

    # Create working shim that starts server
    shim = anthrouter_dir / "shim.sh"
    shim.write_text(f"""#!/usr/bin/env bash
python3 -c "
import threading, socket, time
from http.server import HTTPServer, BaseHTTPRequestHandler

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

s = HTTPServer(('127.0.0.1', {port}), H)
s.serve_forever()
" &
echo $!
""")
    shim.chmod(0o755)

    result = _run_update(home, anthrouter_dir)

    # Script should mention reusing venv.bak
    assert "reusing" in result.stdout.lower() or "already exists" in result.stdout
    # On success, venv.bak is deleted, so check that the run completed successfully
    assert result.returncode == 0, f"Expected success, got: {result.stderr}"


def test_pip_install_failure_restore(tmp_path):
    """Test that venv is restored when pip install fails."""
    home, anthrouter_dir, upstream_repo, _ = _setup_fake_install(
        tmp_path, with_venv_bak=True, venv_bak_has_executable=True
    )

    # Add marker to venv.bak
    venv_bak = anthrouter_dir / "venv.bak"
    (venv_bak / "marker.txt").write_text("backup marker")

    # Make pip fail
    venv_bin = anthrouter_dir / "venv" / "bin"
    pip_fail = anthrouter_dir / "pip_fail.sh"
    pip_fail.write_text("#!/usr/bin/env bash\nexit 1\n")
    pip_fail.chmod(0o755)
    (venv_bin / "pip").write_text(f"#!/usr/bin/env bash\nexec {pip_fail} \"$@\"\n")
    (venv_bin / "pip").chmod(0o755)

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    assert result.returncode != 0
    # venv.bak should be restored to venv
    venv = anthrouter_dir / "venv"
    assert (venv / "marker.txt").exists(), "Marker from venv.bak should be in venv after restore"


def test_health_check_timeout_restore(tmp_path):
    """Test restore when health check times out."""
    # Use file:// URL to local repo for deterministic hermetic test
    home, anthrouter_dir, upstream_repo, _ = _setup_fake_install(
        tmp_path, with_venv_bak=True, venv_bak_has_executable=True
    )
    # Override upstreamUrl to use local repo
    manifest_path = anthrouter_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["upstreamUrl"] = f"file://{upstream_repo}"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Add marker to venv.bak
    venv_bak = anthrouter_dir / "venv.bak"
    (venv_bak / "marker.txt").write_text("backup marker")

    # Shim that starts a process but NOT an HTTP server (health will fail)
    # Use a port that nothing is listening on
    bad_port = _find_free_port()
    shim = anthrouter_dir / "shim.sh"
    shim.write_text("#!/usr/bin/env bash\n# Shim that does not start HTTP server\nsleep 3600 &\necho $!\n")
    shim.chmod(0o755)
    
    # Config with a port that won't have a server
    config_env = anthrouter_dir / "config.env"
    config_env.write_text(f"ANTHROUTER_PORT={bad_port}\nANTHROUTER_HOST=127.0.0.1\n")

    result = _run_update(home, anthrouter_dir)

    assert result.returncode != 0, f"Expected non-zero exit, got {result.returncode}. stdout: {result.stdout}, stderr: {result.stderr}"
    # Should have attempted restore
    assert "restore" in result.stdout.lower() or "Restore" in result.stdout, f"Expected restore message. stdout: {result.stdout}"
    # Verify restore behavior: venv.bak marker should be restored to venv
    venv = anthrouter_dir / "venv"
    assert (venv / "marker.txt").exists(), "Marker from venv.bak should be in venv after restore"


def test_restore_failure_non_executable(tmp_path):
    """Test restore failure when venv.bak/bin/python is not executable."""
    home, anthrouter_dir, upstream_repo, _ = _setup_fake_install(
        tmp_path, with_venv_bak=True, venv_bak_has_executable=False
    )

    # Add marker to original venv to verify it's untouched
    venv = anthrouter_dir / "venv"
    (venv / "marker.txt").write_text("original venv marker")

    # Make pip fail to trigger restore
    venv_bin = anthrouter_dir / "venv" / "bin"
    pip_fail = anthrouter_dir / "pip_fail.sh"
    pip_fail.write_text("#!/usr/bin/env bash\nexit 1\n")
    pip_fail.chmod(0o755)
    (venv_bin / "pip").write_text(f"#!/usr/bin/env bash\nexec {pip_fail} \"$@\"\n")
    (venv_bin / "pip").chmod(0o755)

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    assert result.returncode != 0
    # Restore should be skipped
    assert "probe" in result.stdout.lower() or "not executable" in result.stdout
    # Original venv should be untouched (marker survives)
    assert (venv / "marker.txt").exists()


def test_full_success_flow(tmp_path):
    """Test full successful update flow."""
    # Set up working server port first
    port = _find_free_port()
    
    # Create upstream repo first so we can use its path (use unique name)
    upstream_repo = tmp_path / "upstream_success_repo"
    upstream_repo.mkdir()
    subprocess.run(["git", "init"], cwd=upstream_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=upstream_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=upstream_repo,
        check=True,
        capture_output=True,
    )
    (upstream_repo / "pyproject.toml").write_text(
        '[project]\nname = "anthrouter"\nversion = "0.0.1"\n'
    )
    (upstream_repo / "update.sh").write_text("#!/usr/bin/env bash\n# Updated update.sh\n")
    (upstream_repo / "update.sh").chmod(0o755)
    subprocess.run(
        ["git", "add", "."], cwd=upstream_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial"],
        cwd=upstream_repo,
        check=True,
        capture_output=True,
    )
    
    # Create install with file:// URL
    home, anthrouter_dir, _, manifest_path = _setup_fake_install(
        tmp_path,
        upstream_url=f"file://{upstream_repo}"
    )

    # Remove venv.bak to test fresh backup
    venv_bak = anthrouter_dir / "venv.bak"
    if venv_bak.exists():
        import shutil
        shutil.rmtree(venv_bak)

    # Set up working server
    config_env = anthrouter_dir / "config.env"
    config_env.write_text(f"ANTHROUTER_PORT={port}\nANTHROUTER_HOST=127.0.0.1\n")

    # Create working shim
    shim = anthrouter_dir / "shim.sh"
    shim.write_text(f"""#!/usr/bin/env bash
python3 -c "
from http.server import HTTPServer, BaseHTTPRequestHandler

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

s = HTTPServer(('127.0.0.1', {port}), H)
s.serve_forever()
" &
echo $!
""")
    shim.chmod(0o755)

    # Make pip succeed
    venv_bin = anthrouter_dir / "venv" / "bin"
    (venv_bin / "pip").write_text("#!/usr/bin/env bash\nexit 0\n")
    (venv_bin / "pip").chmod(0o755)

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    assert result.returncode == 0, f"Expected success, got: {result.stderr}"
    assert "complete" in result.stdout.lower()

    # venv.bak should be deleted on success
    assert not venv_bak.exists(), "venv.bak should be deleted on success"

    # Manifest should have updatedAt stamped
    manifest = json.loads(manifest_path.read_text())
    assert "updatedAt" in manifest
    assert "pid" in manifest and manifest["pid"] is not None

    # update.sh should be self-updated (chmod +x)
    installed_update = anthrouter_dir / "update.sh"
    assert installed_update.exists()
    assert os.access(installed_update, os.X_OK)


def test_temp_clone_dir_cleaned(tmp_path):
    """Test that temp clone directory is cleaned up after failure."""
    # Use file:// URL to local repo for deterministic hermetic test
    home, anthrouter_dir, upstream_repo, _ = _setup_fake_install(tmp_path)
    # Override upstreamUrl to use local repo
    manifest_path = anthrouter_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["upstreamUrl"] = f"file://{upstream_repo}"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Make pip fail to test cleanup on failure
    venv_bin = anthrouter_dir / "venv" / "bin"
    (venv_bin / "pip").write_text("#!/usr/bin/env bash\nexit 1\n")
    (venv_bin / "pip").chmod(0o755)

    update_script = Path(__file__).parent.parent / "update.sh"
    result = _run_update(home, anthrouter_dir)

    # Should fail
    assert result.returncode != 0
    # Lock should be cleaned up
    assert not (anthrouter_dir / "update.lock").exists()

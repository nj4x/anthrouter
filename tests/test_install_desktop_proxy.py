"""Tests for the installer's Claude Desktop proxy wiring (ADR-0019).

A full install forks the server and builds a venv, so the wiring is tested in
three ways: pre-flight aborts run the real install.sh (they exit before any
mutation); the cert, health-check and settings steps run install.sh's own
functions, extracted by name; uninstall and update run the real scripts
against a fake ~/.anthrouter.
"""
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"
UNINSTALL = REPO / "uninstall.sh"
UPDATE = REPO / "update.sh"
ANTHROUTER_URL = "http://127.0.0.1:8083"

needs_openssl = pytest.mark.skipif(
    subprocess.run(["which", "openssl"], capture_output=True).returncode != 0,
    reason="openssl not on PATH",
)


def _function_source(name):
    """Return the bash source of one top-level function in install.sh."""
    text = INSTALL.read_text()
    m = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"{name} not found in install.sh"
    return m.group(0)


def _run_functions(tmp_path, body, *, names, env_extra=None):
    """Run `body` in bash with the named install.sh functions defined."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    anthrouter_home = home / ".anthrouter"
    anthrouter_home.mkdir(exist_ok=True)
    prelude = "\n".join(
        [
            "set -euo pipefail",
            f'ANTHROUTER_URL="{ANTHROUTER_URL}"',
            'NODE_CA_BACKUP_KEY="NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP"',
            f'MANIFEST="{anthrouter_home}/manifest.json"',
            f'STEP_OUT="{tmp_path}/step.out"',
            'say() { echo "[anthrouter] $*"; }',
            'die() { echo "[anthrouter] error: $*" >&2; exit 1; }',
            *(_function_source(n) for n in names),
        ]
    )
    script = tmp_path / "harness.sh"
    script.write_text(prelude + "\n" + body + "\n")
    env = {**os.environ, "HOME": str(home), **(env_extra or {})}
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)


# ------------------------------------------------------- pre-flight (real script) ---


def _desktop_env(tmp_path, *, settings=None, launchctl_value="", extra=None):
    """Env for a real install.sh run that sees a Mac with Claude Desktop."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    claude = home / ".claude"
    claude.mkdir(exist_ok=True)
    if settings is not None:
        (claude / "settings.json").write_text(json.dumps(settings))
    app = tmp_path / "Claude.app"
    app.mkdir(exist_ok=True)
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        "#!/usr/bin/env bash\n"
        f'[ "$1" = getenv ] && [ "$2" = HTTPS_PROXY ] && printf %s "{launchctl_value}"\n'
        "exit 0\n"
    )
    launchctl.chmod(0o755)
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("HTTPS_PROXY", "https_proxy", "CLAUDE_CONFIG_DIR", "CAVEMAN_CONFIG")
    }
    env.update(
        HOME=str(home),
        ANTHROUTER_HOME=str(home / ".anthrouter"),
        ANTHROUTER_SHIM_DIR=str(home / "bin"),
        CLAUDE_CONFIG_DIR=str(claude),
        ANTHROUTER_UNAME="Darwin",
        ANTHROUTER_DESKTOP_APPS=str(app),
        ANTHROUTER_LAUNCHCTL=str(launchctl),
        SHELL="/bin/zsh",
    )
    env.update(extra or {})
    return env, home, claude


def _run_install(env):
    return subprocess.run(
        ["bash", str(INSTALL), "--no-browser"],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _assert_untouched(home, claude, settings):
    assert not (home / ".anthrouter").exists()
    assert json.loads((claude / "settings.json").read_text()) == settings


def test_preflight_aborts_on_https_proxy_in_settings(tmp_path):
    settings = {"env": {"HTTPS_PROXY": "http://corp-proxy:3128"}}
    env, home, claude = _desktop_env(tmp_path, settings=settings)
    result = _run_install(env)
    assert result.returncode != 0
    assert "env.HTTPS_PROXY" in result.stderr
    assert "http://corp-proxy:3128" in result.stderr
    _assert_untouched(home, claude, settings)


def test_preflight_aborts_on_lowercase_https_proxy_in_settings(tmp_path):
    settings = {"env": {"https_proxy": ""}}
    env, home, claude = _desktop_env(tmp_path, settings=settings)
    result = _run_install(env)
    assert result.returncode != 0
    assert "env.https_proxy" in result.stderr
    _assert_untouched(home, claude, settings)


def test_preflight_aborts_on_proxy_in_process_env(tmp_path):
    env, home, claude = _desktop_env(
        tmp_path, settings={}, extra={"https_proxy": "http://corp-proxy:3128"}
    )
    result = _run_install(env)
    assert result.returncode != 0
    assert "https_proxy is set in this shell's environment" in result.stderr
    _assert_untouched(home, claude, {})


def test_preflight_aborts_on_proxy_in_launchctl(tmp_path):
    env, home, claude = _desktop_env(
        tmp_path, settings={}, launchctl_value="http://corp-proxy:3128"
    )
    result = _run_install(env)
    assert result.returncode != 0
    assert "launchctl" in result.stderr
    assert "http://corp-proxy:3128" in result.stderr
    _assert_untouched(home, claude, {})


def test_preflight_aborts_on_unreadable_node_extra_ca_certs(tmp_path):
    settings = {"env": {"NODE_EXTRA_CA_CERTS": str(tmp_path / "missing.pem")}}
    env, home, claude = _desktop_env(tmp_path, settings=settings)
    result = _run_install(env)
    assert result.returncode != 0
    assert "cannot be read" in result.stderr
    _assert_untouched(home, claude, settings)


def test_preflight_aborts_on_orphan_node_ca_backup_key(tmp_path):
    settings = {"env": {"NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP": "/x.pem"}}
    env, home, claude = _desktop_env(tmp_path, settings=settings)
    result = _run_install(env)
    assert result.returncode != 0
    assert "NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP" in result.stderr
    assert "no install manifest" in result.stderr
    _assert_untouched(home, claude, settings)


def test_desktop_app_detection(tmp_path):
    app = tmp_path / "Claude.app"
    body = 'desktop_app_present && echo yes || echo no'

    def detect(uname, apps):
        r = _run_functions(
            tmp_path, body, names=["desktop_app_present"],
            env_extra={"ANTHROUTER_UNAME": uname, "ANTHROUTER_DESKTOP_APPS": apps},
        )
        return r.stdout.strip()

    assert detect("Darwin", str(app)) == "no"
    app.mkdir()
    assert detect("Darwin", f"/nonexistent/Claude.app:{app}") == "yes"
    assert detect("Linux", str(app)) == "no"


# ---------------------------------------------------------------- certificates ---


def _openssl(*args):
    return subprocess.run(["openssl", *args], capture_output=True, text=True, check=True).stdout


@needs_openssl
def test_make_certs_modes_and_extensions(tmp_path):
    tls = tmp_path / "home" / ".anthrouter" / "tls"
    result = _run_functions(
        tmp_path, f'desktop_proxy_make_certs "{tls}"', names=["desktop_proxy_make_certs"]
    )
    assert result.returncode == 0, result.stderr

    assert stat.S_IMODE(tls.stat().st_mode) == 0o700
    assert stat.S_IMODE((tls / "ca.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tls / "leaf.key").stat().st_mode) == 0o600
    assert sorted(p.name for p in tls.iterdir()) == ["ca.key", "ca.pem", "leaf.key", "leaf.pem"]

    leaf = str(tls / "leaf.pem")
    ca = str(tls / "ca.pem")
    san = _openssl("x509", "-in", leaf, "-noout", "-ext", "subjectAltName")
    assert "DNS:api.anthropic.com" in san
    assert san.count("DNS:") == 1
    assert "IP" not in san.split("\n", 1)[1]
    assert "TLS Web Server Authentication" in _openssl(
        "x509", "-in", leaf, "-noout", "-ext", "extendedKeyUsage"
    )
    bc = _openssl("x509", "-in", ca, "-noout", "-ext", "basicConstraints")
    assert "critical" in bc
    assert "CA:TRUE" in bc
    assert "pathlen:0" in bc
    for pem in (ca, leaf):
        assert "prime256v1" in _openssl("x509", "-in", pem, "-noout", "-text") or \
            "P-256" in _openssl("x509", "-in", pem, "-noout", "-text")
    assert ": OK" in _openssl("verify", "-CAfile", ca, leaf)

    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(leaf, str(tls / "leaf.key"))


# ---------------------------------------------------------------- health check ---


def _fake_curl(tmp_path, *, code, header="", exit_code=0):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'hdr=""\n'
        'while [ $# -gt 0 ]; do [ "$1" = -D ] && hdr="$2"; shift; done\n'
        f'printf "HTTP/1.1 {code} X\\r\\n{header}" > "$hdr"\n'
        f'[ {exit_code} = 0 ] || {{ echo "curl: (35) TLS failure" >&2; exit {exit_code}; }}\n'
        f'printf {code}\n'
    )
    curl.chmod(0o755)
    return {"PATH": f"{bindir}:{os.environ['PATH']}"}


def _health(tmp_path, **kw):
    return _run_functions(
        tmp_path,
        f'desktop_proxy_health_check "{ANTHROUTER_URL}" /dev/null',
        names=["desktop_proxy_probe", "desktop_proxy_health_check"],
        env_extra=_fake_curl(tmp_path, **kw),
    )


def test_health_check_passes_on_401(tmp_path):
    result = _health(tmp_path, code=401)
    assert result.returncode == 0, result.stderr
    assert "HTTP 401" in result.stdout


def test_health_check_fails_on_anthrouter_error_header(tmp_path):
    result = _health(
        tmp_path, code=502, header="X-Anthrouter-Error: interception-target-unreachable\\r\\n"
    )
    assert result.returncode != 0
    assert "interception-target-unreachable" in result.stderr


def test_health_check_fails_on_tls_error(tmp_path):
    result = _health(tmp_path, code="000", exit_code=35)
    assert result.returncode != 0
    assert "TLS failure" in result.stderr


# --------------------------------------------------------------- settings.json ---


def _wire(tmp_path, settings):
    home = tmp_path / "home"
    (home / ".anthrouter").mkdir(parents=True, exist_ok=True)
    tls = home / ".anthrouter" / "tls"
    tls.mkdir()
    (tls / "ca.pem").write_text("OUR CA\n")
    settings_path = home / "settings.json"
    settings_path.write_text(json.dumps(settings))
    result = _run_functions(
        tmp_path,
        f'desktop_proxy_wire_settings "{settings_path}" "{tls}" || die wire\n'
        'echo "state=$DESKTOP_CA_STATE"',
        names=["mf_write", "mf_set", "desktop_proxy_wire_settings"],
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((home / ".anthrouter" / "manifest.json").read_text())
    return json.loads(settings_path.read_text()), manifest, tls, result


def test_wire_settings_created_path(tmp_path):
    data, manifest, tls, _ = _wire(tmp_path, {"env": {"FOO": "1"}})
    assert data["env"] == {
        "FOO": "1",
        "HTTPS_PROXY": ANTHROUTER_URL,
        "NODE_EXTRA_CA_CERTS": str(tls / "ca.pem"),
    }
    assert manifest["httpsProxy"] == "created"
    assert manifest["nodeExtraCaCerts"] == "created"
    assert not (tls / "ca-bundle.pem").exists()


def test_wire_settings_backed_up_path(tmp_path):
    theirs = tmp_path / "corp.pem"
    theirs.write_text("CORP CA")  # no trailing newline: bundle must still split
    data, manifest, tls, result = _wire(
        tmp_path, {"env": {"NODE_EXTRA_CA_CERTS": str(theirs)}}
    )
    bundle = tls / "ca-bundle.pem"
    assert bundle.read_text() == "CORP CA\nOUR CA\n"
    assert data["env"]["NODE_EXTRA_CA_CERTS"] == str(bundle)
    assert data["env"]["NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP"] == str(theirs)
    assert data["env"]["HTTPS_PROXY"] == ANTHROUTER_URL
    assert manifest["nodeExtraCaCerts"] == "backed_up"
    assert "state=present" in result.stdout


# ------------------------------------------------------------- uninstall (real) ---


def _fake_installed(tmp_path, settings_env, *, ca_slot):
    home = tmp_path / "home"
    ah = home / ".anthrouter"
    tls = ah / "tls"
    tls.mkdir(parents=True)
    (tls / "ca.pem").write_text("OUR CA\n")
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": settings_env}))
    (ah / "config.env").write_text(
        "ANTHROUTER_PORT=8083\n"
        "# >>> anthrouter >>> desktop-proxy\n"
        "ANTHROUTER_CONNECT_PROXY=1\n"
        "# <<< anthrouter <<< desktop-proxy\n"
    )
    (ah / "manifest.json").write_text(json.dumps({
        "claudeSettings": str(settings),
        "settingsSlot": None,
        "desktopProxy": True,
        "tlsDir": str(tls),
        "configEnvDesktopBlock": True,
        "httpsProxy": "created",
        "nodeExtraCaCerts": ca_slot,
        "pid": None,
    }))
    return home, ah, tls, settings


def _uninstall(home, ah):
    env = {**os.environ, "HOME": str(home), "ANTHROUTER_HOME": str(ah)}
    return subprocess.run(["bash", str(UNINSTALL)], capture_output=True, text=True, env=env)


def test_uninstall_reverses_created_path(tmp_path):
    home, ah, tls, settings = _fake_installed(tmp_path, {
        "OTHER": "x",
        "HTTPS_PROXY": ANTHROUTER_URL,
        "NODE_EXTRA_CA_CERTS": str(tmp_path / "home/.anthrouter/tls/ca.pem"),
    }, ca_slot="created")
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["env"] == {"OTHER": "x"}
    assert not tls.exists()
    assert "CONNECT_PROXY" not in (ah / "config.env").read_text()
    assert (ah / "config.env").read_text() == "ANTHROUTER_PORT=8083\n"
    # Idempotent: a second run changes nothing and does not fail.
    assert _uninstall(home, ah).returncode == 0
    assert json.loads(settings.read_text())["env"] == {"OTHER": "x"}


def test_uninstall_reverses_backed_up_path(tmp_path):
    bundle = str(tmp_path / "home/.anthrouter/tls/ca-bundle.pem")
    home, ah, tls, settings = _fake_installed(tmp_path, {
        "HTTPS_PROXY": ANTHROUTER_URL,
        "NODE_EXTRA_CA_CERTS": bundle,
        "NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP": "/corp/ca.pem",
    }, ca_slot="backed_up")
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["env"] == {"NODE_EXTRA_CA_CERTS": "/corp/ca.pem"}
    assert not tls.exists()


def test_uninstall_leaves_user_changed_values_and_warns(tmp_path):
    changed = {
        "HTTPS_PROXY": "http://other:9999",
        "NODE_EXTRA_CA_CERTS": "/user/own.pem",
        "NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP": "/corp/ca.pem",
    }
    home, ah, tls, settings = _fake_installed(tmp_path, dict(changed), ca_slot="backed_up")
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["env"] == changed
    assert "warning" in result.stderr
    assert "HTTPS_PROXY" in result.stderr
    assert "NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP kept" in result.stderr


def test_uninstall_created_ca_changed_by_user_is_left(tmp_path):
    home, ah, tls, settings = _fake_installed(tmp_path, {
        "HTTPS_PROXY": ANTHROUTER_URL,
        "NODE_EXTRA_CA_CERTS": "/user/own.pem",
    }, ca_slot="created")
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["env"] == {"NODE_EXTRA_CA_CERTS": "/user/own.pem"}
    assert "NODE_EXTRA_CA_CERTS is now" in result.stderr


def test_uninstall_refuses_to_delete_foreign_tls_dir(tmp_path):
    home, ah, tls, settings = _fake_installed(tmp_path, {}, ca_slot="created")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    manifest = json.loads((ah / "manifest.json").read_text())
    manifest["tlsDir"] = str(foreign)
    (ah / "manifest.json").write_text(json.dumps(manifest))
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert foreign.exists()
    assert "not deleting it" in result.stderr


def test_uninstall_removes_every_desktop_block(tmp_path):
    home, ah, tls, settings = _fake_installed(tmp_path, {
        "HTTPS_PROXY": ANTHROUTER_URL,
        "NODE_EXTRA_CA_CERTS": str(tmp_path / "home/.anthrouter/tls/ca.pem"),
    }, ca_slot="created")
    config_env = ah / "config.env"
    block = ("# >>> anthrouter >>> desktop-proxy\n"
             "ANTHROUTER_CONNECT_PROXY=1\n"
             "# <<< anthrouter <<< desktop-proxy\n")
    config_env.write_text(config_env.read_text() + block)
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert config_env.read_text() == "ANTHROUTER_PORT=8083\n"

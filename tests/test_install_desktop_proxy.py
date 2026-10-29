"""Tests for the installer's Claude Desktop proxy wiring (ADR-0019, ADR-0020).

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
            'BLOCK_BEGIN="# >>> anthrouter >>>"',
            'BLOCK_END="# <<< anthrouter <<<"',
            f'MANIFEST="{anthrouter_home}/manifest.json"',
            f'STEP_OUT="{tmp_path}/step.out"',
            'say() { echo "[anthrouter] $*"; }',
            'die() { echo "[anthrouter] error: $*" >&2; exit 1; }',
            *(_function_source(n) for n in names),
        ]
    )
    script = tmp_path / "harness.sh"
    script.write_text(prelude + "\n" + body + "\n")
    env = {k: v for k, v in os.environ.items() if k != "NODE_EXTRA_CA_CERTS"}
    env.update(HOME=str(home), **(env_extra or {}))
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)


def _fake_shell(tmp_path, profile, name="fakeshell"):
    """A stand-in for `$SHELL -ilc CMD`: runs *profile*, then CMD."""
    path = tmp_path / name
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"{profile}\n"
        '[ "$1" = -ilc ] || exit 64\n'
        'eval "$2"\n'
    )
    path.chmod(0o755)
    return path


def _pem(tag="x"):
    import base64
    return ("-----BEGIN CERTIFICATE-----\n"
            + base64.b64encode(b'\x30' + tag.encode() * 4).decode() + "\n"
            "-----END CERTIFICATE-----\n")


# ------------------------------------------------------- pre-flight (real script) ---


def _desktop_env(tmp_path, *, settings=None, launchctl_value="", launchctl_ca="",
                 shell_value=None, extra=None):
    """Env for a real install.sh run that sees a Mac with Claude Desktop.

    *shell_value* set stands in a fake login shell whose NODE_EXTRA_CA_CERTS
    is that value; unset, the probe runs a real zsh against the empty fake home.
    """
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
        f'[ "$1" = getenv ] && [ "$2" = NODE_EXTRA_CA_CERTS ] && printf %s "{launchctl_ca}"\n'
        "exit 0\n"
    )
    launchctl.chmod(0o755)
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("HTTPS_PROXY", "https_proxy", "CLAUDE_CONFIG_DIR", "CAVEMAN_CONFIG",
                     "NODE_EXTRA_CA_CERTS")
    }
    if shell_value is not None:
        env["ANTHROUTER_SHELL_PROBE"] = str(_fake_shell(tmp_path, f'export NODE_EXTRA_CA_CERTS="{shell_value}"'))
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
    nc = _openssl("x509", "-in", ca, "-noout", "-ext", "nameConstraints")
    assert "critical" in nc
    assert "Permitted" in nc and "DNS:api.anthropic.com" in nc
    assert nc.count("DNS:") == 1

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
        f'desktop_proxy_wire_settings "{settings_path}" "{tls}" || die wire',
        names=["mf_write", "mf_set", "desktop_proxy_wire_settings"],
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((home / ".anthrouter" / "manifest.json").read_text())
    return json.loads(settings_path.read_text()), manifest, tls, result


def test_wire_settings_created_path(tmp_path):
    """ADR-0020 step 1: the installed value is the bundle path on both paths."""
    data, manifest, tls, _ = _wire(tmp_path, {"env": {"FOO": "1"}})
    bundle = str(tls / "ca-bundle.pem")
    assert data["env"] == {
        "FOO": "1",
        "HTTPS_PROXY": ANTHROUTER_URL,
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "NODE_EXTRA_CA_CERTS": bundle,
    }
    assert manifest["httpsProxy"] == "created"
    assert manifest["noProxy"] == "created"
    assert manifest["noProxyAdded"] == ["127.0.0.1", "localhost", "::1"]
    assert manifest["nodeExtraCaCerts"] == "created"
    assert manifest["nodeExtraCaCertsValue"] == bundle
    # anthrouter is the one bundle builder (ADR-0020 step 5).
    assert not (tls / "ca-bundle.pem").exists()


def test_wire_settings_backed_up_path(tmp_path):
    theirs = tmp_path / "corp.pem"
    theirs.write_text(_pem("corp"))
    data, manifest, tls, result = _wire(
        tmp_path, {"env": {"NODE_EXTRA_CA_CERTS": str(theirs)}}
    )
    bundle = str(tls / "ca-bundle.pem")
    assert data["env"]["NODE_EXTRA_CA_CERTS"] == bundle
    assert data["env"]["NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP"] == str(theirs)
    assert data["env"]["HTTPS_PROXY"] == ANTHROUTER_URL
    assert manifest["nodeExtraCaCerts"] == "backed_up"
    assert manifest["nodeExtraCaCertsValue"] == bundle
    assert not (tls / "ca-bundle.pem").exists()


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


# ------------------------------------------------ ADR-0020 pre-flight (real script) ---


def test_preflight_aborts_on_node_extra_ca_certs_in_launchctl(tmp_path):
    env, home, claude = _desktop_env(tmp_path, settings={}, launchctl_ca="/corp/bundle.pem")
    result = _run_install(env)
    assert result.returncode != 0
    assert "/corp/bundle.pem" in result.stderr
    assert "launchctl unsetenv NODE_EXTRA_CA_CERTS" in result.stderr
    assert "--no-desktop-proxy" in result.stderr
    _assert_untouched(home, claude, {})


def test_preflight_aborts_on_non_pem_settings_value(tmp_path):
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate\n")
    settings = {"env": {"NODE_EXTRA_CA_CERTS": str(junk)}}
    env, home, claude = _desktop_env(tmp_path, settings=settings)
    result = _run_install(env)
    assert result.returncode != 0
    assert str(junk) in result.stderr and "cannot be read" in result.stderr
    _assert_untouched(home, claude, settings)


def test_preflight_aborts_on_colon_in_settings_value(tmp_path):
    odd = tmp_path / "a:b.pem"
    odd.write_text(_pem())
    settings = {"env": {"NODE_EXTRA_CA_CERTS": str(odd)}}
    env, home, claude = _desktop_env(tmp_path, settings=settings)
    result = _run_install(env)
    assert result.returncode != 0
    assert str(odd) in result.stderr and "':'" in result.stderr
    _assert_untouched(home, claude, settings)


@pytest.mark.parametrize("content, needle", [
    (None, "cannot be read"),
    ("junk\n", "cannot be read"),
    (_pem() + "-----BEGIN CERTIFICATE-----\nAAAA\n", "ends inside"),
])
def test_preflight_aborts_on_unusable_shell_value(tmp_path, content, needle):
    src = tmp_path / "shell.pem"
    if content is not None:
        src.write_text(content)
    env, home, claude = _desktop_env(tmp_path, settings={}, shell_value=str(src))
    result = _run_install(env)
    assert result.returncode != 0
    assert str(src) in result.stderr and needle in result.stderr
    _assert_untouched(home, claude, {})


def test_preflight_aborts_on_colon_in_shell_value(tmp_path):
    env, home, claude = _desktop_env(tmp_path, settings={}, shell_value="/a.pem:/b.pem")
    result = _run_install(env)
    assert result.returncode != 0
    assert "/a.pem:/b.pem" in result.stderr and "':'" in result.stderr
    _assert_untouched(home, claude, {})


def test_preflight_aborts_on_unsupported_shell_with_a_value(tmp_path):
    src = tmp_path / "shell.pem"
    src.write_text(_pem())
    env, home, claude = _desktop_env(
        tmp_path, settings={}, shell_value=str(src), extra={"SHELL": "/usr/local/bin/fish"})
    result = _run_install(env)
    assert result.returncode != 0
    assert "/usr/local/bin/fish" in result.stderr and "bash or zsh" in result.stderr
    _assert_untouched(home, claude, {})


# ---------------------------------------------------------------- shell probe ---


def _probe(tmp_path, profile, **env_extra):
    shell = _fake_shell(tmp_path, profile)
    return _run_functions(
        tmp_path, 'desktop_shell_probe || exit $?\necho "value=[$SHELL_PROBE_VALUE]"',
        names=["desktop_shell_probe"],
        env_extra={"ANTHROUTER_SHELL_PROBE": str(shell), **env_extra},
    )


def test_probe_takes_only_the_text_between_the_sentinels(tmp_path):
    result = _probe(tmp_path, 'echo "welcome banner"; echo "warn" >&2\n'
                              'export NODE_EXTRA_CA_CERTS="/Library/App Support/x.pem"')
    assert result.returncode == 0, result.stderr
    assert "value=[/Library/App Support/x.pem]" in result.stdout


def test_probe_ignores_a_value_inherited_from_the_installer(tmp_path):
    result = _probe(tmp_path, ":", NODE_EXTRA_CA_CERTS="/inherited.pem")
    assert result.returncode == 0, result.stderr
    assert "value=[]" in result.stdout


def test_probe_aborts_on_non_zero_exit(tmp_path):
    result = _probe(tmp_path, "echo broken profile >&2; exit 3")
    assert result.returncode != 0
    assert "exited with status 3" in result.stderr and "broken profile" in result.stderr


def test_probe_aborts_on_timeout(tmp_path):
    result = _probe(tmp_path, "sleep 30", ANTHROUTER_SHELL_PROBE_TIMEOUT="1")
    assert result.returncode != 0
    assert "did not finish within 1 s" in result.stderr


def test_probe_aborts_when_the_sentinels_are_missing(tmp_path):
    shell = tmp_path / "noprint"
    shell.write_text("#!/usr/bin/env bash\necho raw-profile-output\n")
    shell.chmod(0o755)
    result = _run_functions(
        tmp_path, "desktop_shell_probe", names=["desktop_shell_probe"],
        env_extra={"ANTHROUTER_SHELL_PROBE": str(shell)},
    )
    assert result.returncode != 0
    assert "raw-profile-output" in result.stderr


def test_probe_runs_a_real_login_bash(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_profile").write_text('export NODE_EXTRA_CA_CERTS="/from/profile.pem"\n')
    result = _run_functions(
        tmp_path, 'desktop_shell_probe\necho "value=[$SHELL_PROBE_VALUE]"',
        names=["desktop_shell_probe"], env_extra={"SHELL": "/bin/bash"},
    )
    assert result.returncode == 0, result.stderr
    assert "value=[/from/profile.pem]" in result.stdout


# ----------------------------------------------------------- readable-PEM check ---


@pytest.mark.parametrize("content, ok", [
    (_pem(), True),
    (_pem("a") + _pem("b"), True),
    ("", False),
    ("junk\n", False),
    (_pem() + "-----BEGIN CERTIFICATE-----\nAAAA\n", False),
    ("-----BEGIN CERTIFICATE-----\n!!!!\n-----END CERTIFICATE-----\n", False),
    ("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n", False),
    ("-----BEGIN CERTIFICATE-----\naGVsbG8=\n-----END CERTIFICATE-----\n", False),
    ("-----BEGIN PRIVATE KEY-----\nMAA=\n-----END PRIVATE KEY-----\n", False),
    (_pem().replace("\n", "\r\n"), True),
])
def test_installer_and_anthrouter_agree_on_a_readable_bundle(tmp_path, content, ok):
    from anthrouter.interception import pem_problem
    src = tmp_path / "src.pem"
    src.write_text(content)
    result = _run_functions(
        tmp_path, f'desktop_ca_source_check label "{src}"',
        names=["desktop_ca_source_check"],
    )
    assert (result.returncode == 0) is ok, result.stderr
    assert (pem_problem(content.encode()) is None) is ok


# ------------------------------------------------------- config.env block ---


def test_config_block_carries_the_sources_through_the_shim(tmp_path):
    from anthrouter.config import parse_args
    tls = tmp_path / "tls"
    for sources, expected in (
        ("", ()),
        ("/Library/Application Support/Netskope/b.pem:/c.pem",
         ("/Library/Application Support/Netskope/b.pem", "/c.pem")),
    ):
        result = _run_functions(
            tmp_path,
            f'TOPOLOGY=direct HOME_DIR="$HOME/.anthrouter" TLS_DIR="{tls}" '
            f'CA_SOURCES="{sources}" desktop_proxy_config_block',
            names=["desktop_proxy_config_block"],
        )
        assert result.returncode == 0, result.stderr
        block = result.stdout
        assert block.startswith("# >>> anthrouter >>> desktop-proxy\n")
        assert "ANTHROUTER_INTERCEPTION_CA_SOURCES=" in block
        env_file = tmp_path / "config.env"
        env_file.write_text(block)
        sourced = subprocess.run(
            ["bash", "-c", f'set -a; . "{env_file}"; printf %s "$ANTHROUTER_INTERCEPTION_CA_SOURCES"'],
            capture_output=True, text=True, check=True,
        ).stdout
        assert sourced == sources
        os.environ["ANTHROUTER_INTERCEPTION_CA_SOURCES"] = sourced
        try:
            assert parse_args([]).interception_ca_sources == expected
        finally:
            del os.environ["ANTHROUTER_INTERCEPTION_CA_SOURCES"]


# ------------------------------------------------------------ bundle check ---


@pytest.mark.parametrize("bundle, ok", [
    (None, False),
    ("OUR CA\n", True),
    (_pem("corp") + "OUR CA\n", True),
    ("OUR CA\n" + _pem("corp"), False),
])
def test_bundle_check_requires_a_bundle_ending_with_ca_pem(tmp_path, bundle, ok):
    tls = tmp_path / "tls"
    tls.mkdir()
    (tls / "ca.pem").write_text("OUR CA\n")
    if bundle is not None:
        (tls / "ca-bundle.pem").write_text(bundle)
    result = _run_functions(tmp_path, f'desktop_ca_bundle_check "{tls}"',
                            names=["desktop_ca_bundle_check"])
    assert (result.returncode == 0) is ok, result.stderr


# ------------------------------------------------------------- profile block ---


def _profile_write_and_verify(tmp_path, profile_text, bash_profile):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".anthrouter").mkdir(exist_ok=True)
    profile = home / ".bashrc"
    profile.write_text(profile_text)
    (home / ".bash_profile").write_text(bash_profile)
    tls = home / ".anthrouter" / "tls"
    result = _run_functions(
        tmp_path,
        f'desktop_ca_profile_write "{profile}" "{tls}" || die write\n'
        f'desktop_ca_profile_verify "{tls}/ca-bundle.pem"',
        names=["mf_write", "mf_set", "desktop_shell_probe",
               "desktop_ca_profile_write", "desktop_ca_profile_verify"],
        env_extra={"SHELL": "/bin/bash"},
    )
    manifest = json.loads((home / ".anthrouter" / "manifest.json").read_text())
    return result, profile, tls, manifest


def test_profile_block_is_appended_with_markers_and_verified(tmp_path):
    user_line = 'export NODE_EXTRA_CA_CERTS="/corp/bundle.pem"'
    result, profile, tls, manifest = _profile_write_and_verify(
        tmp_path, user_line, bash_profile=". ~/.bashrc\n")
    assert result.returncode == 0, result.stderr
    assert profile.read_text() == (
        f"{user_line}\n"
        "# >>> anthrouter >>> ca-bundle\n"
        f'export NODE_EXTRA_CA_CERTS="{tls}/ca-bundle.pem"\n'
        "# <<< anthrouter <<< ca-bundle\n"
    )
    assert manifest["caBundleProfileFile"] == str(profile)


def test_profile_verify_fails_and_names_the_winning_value(tmp_path):
    result, *_ = _profile_write_and_verify(
        tmp_path, "", bash_profile='. ~/.bashrc\nexport NODE_EXTRA_CA_CERTS="/late/zlogin.pem"\n')
    assert result.returncode != 0
    assert "/late/zlogin.pem" in result.stderr


def test_profile_verify_on_bash_names_bash_profile_when_it_skips_bashrc(tmp_path):
    result, *_ = _profile_write_and_verify(tmp_path, "", bash_profile=":\n")
    assert result.returncode != 0
    assert ".bash_profile" in result.stderr and ".bashrc" in result.stderr


# --------------------------------------------------- ADR-0020 uninstall (real) ---


def _fake_adr20_install(tmp_path, *, profile_lines=None, ca_slot="created", settings_env=None):
    home, ah, tls, settings = _fake_installed(tmp_path, {}, ca_slot=ca_slot)
    bundle = str(tls / "ca-bundle.pem")
    (tls / "ca-bundle.pem").write_text("BUNDLE\n")
    env = {"HTTPS_PROXY": ANTHROUTER_URL, "NODE_EXTRA_CA_CERTS": bundle}
    env.update(settings_env or {})
    settings.write_text(json.dumps({"env": env}))
    profile = home / ".zshrc"
    lines = profile_lines if profile_lines is not None else [
        'export NODE_EXTRA_CA_CERTS="/corp/bundle.pem"',
        "# >>> anthrouter >>> ca-bundle",
        f'export NODE_EXTRA_CA_CERTS="{bundle}"',
        "# <<< anthrouter <<< ca-bundle",
    ]
    profile.write_text("\n".join(lines) + "\n")
    manifest = json.loads((ah / "manifest.json").read_text())
    manifest.update(caBundleProfileFile=str(profile), nodeExtraCaCertsValue=bundle,
                    caNameConstrained=True)
    (ah / "manifest.json").write_text(json.dumps(manifest))
    return home, ah, tls, settings, profile


def test_uninstall_removes_profile_block_settings_key_and_tls(tmp_path):
    home, ah, tls, settings, profile = _fake_adr20_install(tmp_path)
    env = {**os.environ, "HOME": str(home), "ANTHROUTER_HOME": str(ah), "SHELL": "/bin/bash"}
    result = subprocess.run(["bash", str(UNINSTALL)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert profile.read_text() == 'export NODE_EXTRA_CA_CERTS="/corp/bundle.pem"\n'
    assert json.loads(settings.read_text())["env"] == {}
    assert not tls.exists()
    assert not (ah / "manifest.json").exists()


def test_uninstall_restores_backed_up_value_via_the_recorded_installed_value(tmp_path):
    home, ah, tls, settings, _ = _fake_adr20_install(
        tmp_path, ca_slot="backed_up",
        settings_env={"NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP": "/corp/settings.pem"})
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["env"] == {"NODE_EXTRA_CA_CERTS": "/corp/settings.pem"}


def test_uninstall_keeps_tls_and_manifest_while_a_line_names_the_bundle(tmp_path):
    # Edited markers: the export survives outside the block.
    bundle_line = 'export NODE_EXTRA_CA_CERTS="{}"'
    home, ah, tls, settings, profile = _fake_adr20_install(tmp_path, profile_lines=[])
    line = bundle_line.format(tls / "ca-bundle.pem")
    profile.write_text(f"# >>> anthrouter >>> ca-bundle (edited)\n{line}\n")
    result = _uninstall(home, ah)
    assert result.returncode != 0
    assert tls.exists() and (ah / "manifest.json").exists()
    assert str(profile) in result.stderr and line in result.stderr
    assert "uninstall.sh" in result.stderr
    profile.write_text("")
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert not tls.exists()


def test_uninstall_removes_only_the_exact_block_when_its_end_marker_was_edited(tmp_path):
    home, ah, tls, settings, profile = _fake_adr20_install(tmp_path, profile_lines=[])
    bundle = tls / "ca-bundle.pem"
    profile.write_text(
        "# >>> anthrouter >>> ca-bundle\n"
        f'export NODE_EXTRA_CA_CERTS="{bundle}"\n'
        "# end edited\n"
        "user line\n"
        "# <<< anthrouter <<< ca-bundle\n"
    )
    result = _uninstall(home, ah)
    assert "user line" in profile.read_text()
    assert result.returncode != 0 and tls.exists()


def test_uninstall_keeps_tls_when_bashrc_names_the_bundle(tmp_path):
    home, ah, tls, settings, profile = _fake_adr20_install(tmp_path)
    (home / ".bashrc").write_text(f'export NODE_EXTRA_CA_CERTS={tls}/ca-bundle.pem\n')
    result = _uninstall(home, ah)
    assert result.returncode != 0
    assert tls.exists()
    assert ".bashrc" in result.stderr


def test_uninstall_keeps_every_other_profile_byte_and_the_symlink(tmp_path):
    home, ah, tls, settings, profile = _fake_adr20_install(tmp_path, profile_lines=[])
    bundle = tls / "ca-bundle.pem"
    real = tmp_path / "dotfiles" / "zshrc"
    real.parent.mkdir()
    before = "# х ok \u00c5 \U0001F605\r\nexport A=1\n".encode() + b"\xff latin\n"
    real.write_bytes(
        before
        + b"# >>> anthrouter >>> ca-bundle\n"
        + f'export NODE_EXTRA_CA_CERTS="{bundle}"\n'.encode()
        + b"# <<< anthrouter <<< ca-bundle\n"
        + b"tail\n"
    )
    real.chmod(0o600)
    profile.unlink()
    profile.symlink_to(real)
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert profile.is_symlink()
    assert real.read_bytes() == before + b"tail\n"
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert not tls.exists()


def test_uninstall_leaves_a_block_whose_export_the_user_edited(tmp_path):
    lines = ["# >>> anthrouter >>> ca-bundle",
             'export NODE_EXTRA_CA_CERTS="/corp/new.pem"',
             "# <<< anthrouter <<< ca-bundle"]
    home, ah, tls, settings, profile = _fake_adr20_install(tmp_path, profile_lines=lines)
    assert _uninstall(home, ah).returncode == 0
    assert profile.read_text() == "\n".join(lines) + "\n"


# ------------------------------------------------------------------- NO_PROXY ---


def test_wire_settings_merges_into_an_existing_no_proxy(tmp_path):
    """Claude Code sends http:// base-URL traffic through HTTPS_PROXY too, so loopback must be exempt."""
    data, manifest, _, _ = _wire(tmp_path, {"env": {"NO_PROXY": "corp.internal, localhost"}})
    assert data["env"]["NO_PROXY"] == "corp.internal, localhost,127.0.0.1,::1"
    assert manifest["noProxy"] == "merged"
    assert manifest["noProxyAdded"] == ["127.0.0.1", "::1"]


def test_uninstall_removes_only_the_no_proxy_entries_install_added(tmp_path):
    home, ah, tls, settings = _fake_installed(tmp_path, {
        "HTTPS_PROXY": ANTHROUTER_URL,
        "NO_PROXY": "corp.internal, localhost,127.0.0.1,::1,user.added",
    }, ca_slot="created")
    manifest = json.loads((ah / "manifest.json").read_text())
    manifest.update(noProxy="merged", noProxyAdded=["127.0.0.1", "::1"])
    (ah / "manifest.json").write_text(json.dumps(manifest))
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["env"] == {
        "NO_PROXY": "corp.internal,localhost,user.added"}


def test_uninstall_deletes_a_created_no_proxy_left_empty(tmp_path):
    home, ah, tls, settings = _fake_installed(tmp_path, {
        "OTHER": "x",
        "HTTPS_PROXY": ANTHROUTER_URL,
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }, ca_slot="created")
    manifest = json.loads((ah / "manifest.json").read_text())
    manifest.update(noProxy="created", noProxyAdded=["127.0.0.1", "localhost", "::1"])
    (ah / "manifest.json").write_text(json.dumps(manifest))
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["env"] == {"OTHER": "x"}
    assert _uninstall(home, ah).returncode == 0


def test_uninstall_survives_a_no_proxy_that_is_no_longer_a_string(tmp_path):
    home, ah, tls, settings = _fake_installed(tmp_path, {
        "HTTPS_PROXY": ANTHROUTER_URL,
        "NO_PROXY": ["127.0.0.1"],
    }, ca_slot="created")
    manifest = json.loads((ah / "manifest.json").read_text())
    manifest.update(noProxy="merged", noProxyAdded=["127.0.0.1"])
    (ah / "manifest.json").write_text(json.dumps(manifest))
    result = _uninstall(home, ah)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text())["env"] == {"NO_PROXY": ["127.0.0.1"]}
    assert not tls.exists()
    assert "NO_PROXY is now" in result.stderr

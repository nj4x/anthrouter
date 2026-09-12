"""_maybe_inject_system_trust: --tls-system-trust startup wiring."""

import logging
import sys
import types

from anthrouter.__main__ import _maybe_inject_system_trust
from anthrouter.config import Config


def test_disabled_by_default_is_a_noop(caplog):
    cfg = Config(tls_system_trust=False)
    assert _maybe_inject_system_trust(cfg, logging.getLogger(__name__)) is True
    assert caplog.records == []


def test_enabled_without_truststore_installed_fails_closed(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, 'truststore', None)  # forces ImportError
    cfg = Config(tls_system_trust=True)
    with caplog.at_level(logging.ERROR):
        result = _maybe_inject_system_trust(cfg, logging.getLogger(__name__))
    assert result is False
    assert 'truststore is not installed' in caplog.text


def test_enabled_with_truststore_installed_injects(monkeypatch, caplog):
    calls = []
    fake_truststore = types.ModuleType('truststore')
    fake_truststore.inject_into_ssl = lambda: calls.append('injected')
    monkeypatch.setitem(sys.modules, 'truststore', fake_truststore)

    cfg = Config(tls_system_trust=True)
    with caplog.at_level(logging.INFO):
        result = _maybe_inject_system_trust(cfg, logging.getLogger(__name__))

    assert result is True
    assert calls == ['injected']
    assert 'delegated to the OS trust store' in caplog.text

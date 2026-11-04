"""ADR-0032: a streaming request with a ``thread`` field holds its status line."""

import http.client
import json
import logging
import socket
import time

import pytest

from tests.conftest import SESSION, SSE_BODY, _FakeUpstream, db_rows

EXPIRED = 'No thread state was found for the requested `previous_message_id`.'
DELAY = 0.4


def _payload(thread=True, **extra):
    payload = {
        'model': 'claude-haiku-4-5-20251001', 'stream': True,
        'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'hi'}],
        **extra,
    }
    if thread:
        payload['thread'] = {'type': 'continue', 'previous_message_id': 'msg_A'}
    return payload


def _stream(server, payload):
    """Return ``(status, headers, body, seconds_to_status_line)``."""
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    start = time.monotonic()
    conn.request('POST', '/v1/messages', body=json.dumps(payload).encode(),
                 headers={'Content-Type': 'application/json', 'x-api-key': 'sk-test'})
    resp = conn.getresponse()
    to_status = time.monotonic() - start
    body = resp.read().decode()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers, body, to_status


def _wait_for_row(server, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = db_rows(server)
        if rows:
            return rows[0]
        time.sleep(0.02)
    raise AssertionError('no requests row recorded')


def test_first_chunk_before_the_interval_commits_200_then_the_stream(proxy):
    _FakeUpstream.response_delay = DELAY
    server = proxy(sse_keepalive_interval=5)
    status, headers, body, to_status = _stream(server, _payload())
    assert status == 200
    assert headers['content-type'] == 'text/event-stream'
    assert to_status >= DELAY
    assert body == SSE_BODY


def test_interval_first_commits_200_with_the_first_keepalive(proxy):
    _FakeUpstream.response_delay = DELAY
    server = proxy(sse_keepalive_interval=0.1)
    status, _headers, body, to_status = _stream(server, _payload())
    assert status == 200
    assert to_status < DELAY
    assert body.startswith(': keepalive\n\n')
    assert body.endswith(SSE_BODY)


def test_keepalive_off_holds_until_the_first_chunk_with_no_keepalive(proxy):
    _FakeUpstream.response_delay = DELAY
    server = proxy(sse_keepalive_interval=0)
    status, _headers, body, to_status = _stream(server, _payload())
    assert status == 200
    assert to_status >= DELAY
    assert body == SSE_BODY
    assert 'keepalive' not in body


@pytest.mark.parametrize('interval', [0, 5])
def test_request_error_before_commit_is_the_non_streaming_response(
        proxy, monkeypatch, caplog, interval):
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _FakeUpstream.status = 429
    _FakeUpstream.error_type = 'rate_limit_error'
    _FakeUpstream.error_message = 'slow down'
    _FakeUpstream.error_headers = {'retry-after': '7'}
    server = proxy(sse_keepalive_interval=interval)
    with caplog.at_level(logging.INFO, logger='anthrouter.handlers'):
        status, headers, body, _to_status = _stream(server, _payload())
    assert status == 429
    assert headers['content-type'] == 'application/json'
    assert headers['retry-after'] == '7'
    assert json.loads(body) == {
        'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'slow down'}}
    row = _wait_for_row(server)
    assert row['status'] == 'rate_limited'
    assert row['error'] == '429 — rate_limit_error'
    assert any('"POST /v1/messages HTTP/1.1" 429' in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('interval', [0, 5])
def test_other_failure_before_commit_is_a_502_api_error(proxy, interval):
    _FakeUpstream.stream_malformed = True
    server = proxy(sse_keepalive_interval=interval)
    status, headers, body, _to_status = _stream(server, _payload())
    assert status == 502
    assert headers['content-type'] == 'application/json'
    assert json.loads(body)['error'] == {
        'type': 'api_error', 'message': 'Upstream request failed'}
    row = _wait_for_row(server)
    assert row['status'] == 'error'
    assert row['error'] == '502 — upstream_failure'


@pytest.mark.parametrize('interval', [0, 5])
def test_zero_chunks_commit_200_with_an_empty_body(proxy, interval):
    _FakeUpstream.stream_zero_chunks = True
    server = proxy(sse_keepalive_interval=interval)
    status, headers, body, _to_status = _stream(server, _payload())
    assert status == 200
    assert headers['content-type'] == 'text/event-stream'
    assert body == ''


def test_error_after_commit_stays_an_sse_error_event(proxy):
    _FakeUpstream.stream_error = True
    server = proxy(sse_keepalive_interval=5)
    status, _headers, body, _to_status = _stream(server, _payload())
    assert status == 200
    assert body.startswith('event: message_start\n')
    assert 'event: error\n' in body
    assert _wait_for_row(server)['status'] == 'error'


def test_client_disconnect_during_the_hold_writes_nothing_and_finishes(proxy):
    _FakeUpstream.response_delay = DELAY
    server = proxy(sse_keepalive_interval=0)
    host, port = server.server_address[:2]
    body = json.dumps(_payload()).encode()
    sock = socket.create_connection((host, port), timeout=5)
    sock.sendall(
        b'POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
        b'x-api-key: sk-test\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n'
        + body)
    sock.settimeout(DELAY / 4)
    with pytest.raises(socket.timeout):
        sock.recv(1)  # nothing written during the hold
    sock.close()
    _wait_for_row(server, timeout=DELAY + 5)
    assert len(_FakeUpstream.received) == 1
    # The handler survived and serves the next request.
    _FakeUpstream.response_delay = 0.0
    status, _headers, out, _to_status = _stream(server, _payload())
    assert status == 200
    assert out == SSE_BODY


def test_stream_without_thread_commits_200_before_priming(proxy):
    _FakeUpstream.response_delay = DELAY
    server = proxy(sse_keepalive_interval=5)
    status, _headers, body, to_status = _stream(server, _payload(thread=False))
    assert status == 200
    assert to_status < DELAY
    assert body == SSE_BODY


def test_stream_without_thread_still_sends_a_pre_stream_error_in_band(proxy):
    _FakeUpstream.status = 400
    _FakeUpstream.error_type = 'invalid_request_error'
    server = proxy(sse_keepalive_interval=5)
    status, _headers, body, _to_status = _stream(server, _payload(thread=False))
    assert status == 200
    assert body.startswith('event: error\n')


def _expire():
    _FakeUpstream.status = 404
    _FakeUpstream.error_type = 'not_found_error'
    _FakeUpstream.error_message = EXPIRED


def test_expired_thread_404_before_the_first_keepalive_is_an_http_400(proxy):
    _expire()
    server = proxy(sse_keepalive_interval=5)
    status, headers, body, _to_status = _stream(server, _payload())
    assert status == 400
    assert headers['content-type'] == 'application/json'
    assert json.loads(body)['error'] == {'type': 'invalid_request_error', 'message': EXPIRED}
    row = _wait_for_row(server)
    assert row['status'] == 'error'
    assert row['error'] == '404 — not_found_error (sent to client as 400)'


def test_expired_thread_404_after_the_first_keepalive_is_an_sse_error_event(proxy):
    _expire()
    _FakeUpstream.response_delay = DELAY
    server = proxy(sse_keepalive_interval=0.1)
    status, _headers, body, _to_status = _stream(server, _payload())
    assert status == 200
    assert body.startswith(': keepalive\n\n')
    data = next(line for line in body.splitlines() if line.startswith('data:'))
    assert json.loads(data[len('data:'):])['error'] == {
        'type': 'invalid_request_error', 'message': EXPIRED}
    assert _wait_for_row(server)['error'] == '404 — not_found_error (sent to client as 400)'

"""Shared HTTP harness: a fake upstream, a live proxy, and request helpers."""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from anthrouter.config import Config
from anthrouter.server import create_server

SESSION = json.dumps({'session_id': '11111111-2222-3333-4444-555555555555'})

# Classifies 'deep' under rules mode, so a sonnet request is actually rewritten.
DEEP_PROMPT = 'architect a distributed system'

SSE_BODY = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_1","type":"message",'
    '"role":"assistant","content":[],"model":"claude-haiku-4-5-20251001",'
    '"usage":{"input_tokens":100,"cache_read_input_tokens":900}}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hello"}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"input_tokens":100,"output_tokens":7,"cache_read_input_tokens":900}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)


class _FakeUpstream(BaseHTTPRequestHandler):
    """Records what it received and replies with whatever the test configured."""

    received: list = []
    status = 200
    json_body: dict = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        payload = json.loads(body or b'{}')
        type(self).received.append({
            'path': self.path,
            'payload': payload,
            'headers': {k.lower(): v for k, v in self.headers.items()},
        })

        if type(self).status != 200:
            data = json.dumps({'type': 'error', 'error': {
                'type': 'api_error', 'message': 'upstream exploded'}}).encode()
            self.send_response(type(self).status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if payload.get('stream'):
            data = SSE_BODY.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self._ratelimit_headers()
            self.end_headers()
            self.wfile.write(data)
            return

        body_out = dict(type(self).json_body)
        body_out.setdefault('model', payload.get('model'))
        data = json.dumps(body_out).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self._ratelimit_headers()
        self.end_headers()
        self.wfile.write(data)

    def _ratelimit_headers(self):
        self.send_header('anthropic-ratelimit-requests-remaining', '42')
        self.send_header('anthropic-ratelimit-tokens-remaining', '13000')
        self.send_header('anthropic-ratelimit-tokens-reset', '2026-08-31T12:00:00Z')

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    _FakeUpstream.received = []
    _FakeUpstream.status = 200
    _FakeUpstream.json_body = {
        'id': 'msg_up', 'type': 'message', 'role': 'assistant',
        'content': [{'type': 'text', 'text': 'hi there'}],
        'usage': {'input_tokens': 100, 'output_tokens': 7,
                  'cache_read_input_tokens': 900},
    }
    server = ThreadingHTTPServer(('127.0.0.1', 0), _FakeUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy(upstream, tmp_path):
    def _build(**overrides):
        cfg = Config(**{
            'host': '127.0.0.1',
            'port': 0,
            'upstream_base_url': f'http://127.0.0.1:{upstream.server_address[1]}',
            'db_path': str(tmp_path / 'anthrouter.db'),
            'sse_keepalive_interval': 0,
            **overrides,
        })
        server = create_server(cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        server.base_url = f'http://127.0.0.1:{server.server_address[1]}'
        started.append(server)
        return server

    started: list = []
    yield _build
    for server in started:
        server.shutdown()
        server.server_close()


def post(server, path, payload, headers=None, raw=False):
    request = urllib.request.Request(
        server.base_url + path,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'x-api-key': 'sk-test',
                 **(headers or {})},
        method='POST',
    )
    with urllib.request.urlopen(request) as resp:
        body = resp.read().decode()
    return body if raw else json.loads(body)


def db_rows(server):
    return server.RequestHandlerClass.request_db.get_requests(limit=10)



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


STREAM_ERROR_BODY = (
    'event: error\n'
    'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
)


class _FakeUpstream(BaseHTTPRequestHandler):
    """Records what it received and replies with whatever the test configured."""

    received: list = []
    status = 200
    json_body: dict = {}
    extra_headers: dict = {}
    error_type: str | None = 'api_error'
    error_message = 'upstream exploded'
    error_raw: bytes | None = None
    error_headers: dict = {}
    # Per-response success ids, consumed in order: the JSON body's top-level
    # ``id`` or the stream's ``message_start.message.id``.  Exhausted, the
    # defaults stand.
    message_ids: list = []
    # Stream a ``message_start`` then an ``error`` event, with no ``message_stop``.
    stream_error = False
    # Text of the stream's single ``text_delta``, JSON-escaped into SSE_BODY.
    stream_text = 'hello'

    def _next_id(self):
        ids = type(self).message_ids
        return ids.pop(0) if ids else None

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        payload = json.loads(body or b'{}')
        type(self).received.append({
            'path': self.path,
            'payload': payload,
            'headers': {k.lower(): v for k, v in self.headers.items()},
        })

        if type(self).status != 200:
            cls = type(self)
            if cls.error_raw is not None:
                data = cls.error_raw
            else:
                error = {'message': cls.error_message}
                if cls.error_type is not None:
                    error['type'] = cls.error_type
                data = json.dumps({'type': 'error', 'error': error}).encode()
            self.send_response(cls.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            for name, value in {**cls.extra_headers, **cls.error_headers}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)
            return

        if payload.get('stream'):
            cls = type(self)
            text = SSE_BODY
            message_id = self._next_id()
            if message_id is not None:
                assert '"id":"msg_1"' in text, 'SSE_BODY lost its message id literal'
                text = text.replace('"id":"msg_1"', f'"id":"{message_id}"', 1)
            if cls.stream_text != 'hello':
                text = text.replace('"text":"hello"', f'"text":{json.dumps(cls.stream_text)}', 1)
            if cls.stream_error:
                text = text.split('event: content_block_delta', 1)[0] + STREAM_ERROR_BODY
            data = text.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self._ratelimit_headers()
            self.end_headers()
            self.wfile.write(data)
            return

        body_out = dict(type(self).json_body)
        body_out.setdefault('model', payload.get('model'))
        message_id = self._next_id()
        if message_id is not None:
            body_out['id'] = message_id
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
        for name, value in type(self).extra_headers.items():
            self.send_header(name, value)

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    _FakeUpstream.received = []
    _FakeUpstream.status = 200
    _FakeUpstream.extra_headers = {}
    _FakeUpstream.error_type = 'api_error'
    _FakeUpstream.error_message = 'upstream exploded'
    _FakeUpstream.error_raw = None
    _FakeUpstream.error_headers = {}
    _FakeUpstream.message_ids = []
    _FakeUpstream.stream_error = False
    _FakeUpstream.stream_text = 'hello'
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

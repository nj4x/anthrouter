import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from anthrouter.http_util import UpstreamTarget
from anthrouter.mapper.anthropic_transform import build_body, merge_betas
from anthrouter.mapper.common import AnthropicRequestError
from anthrouter.transport import (
    AnthropicTransport,
    _request_headers,
    extract_client_credentials,
)


# ---------------------------------------------------------------------------
# UpstreamTarget
# ---------------------------------------------------------------------------

def test_https_target_defaults_to_no_explicit_port():
    target = UpstreamTarget('https://api.anthropic.com')
    assert (target.scheme, target.host, target.port) == ('https', 'api.anthropic.com', None)
    assert target.path('/v1/messages') == '/v1/messages'


def test_http_scheme_is_preserved():
    target = UpstreamTarget('http://127.0.0.1:8080')
    assert target.scheme == 'http'
    assert target.port == 8080


def test_path_component_prefixes_outbound_paths():
    target = UpstreamTarget('https://gw.example.com/anthropic/')
    assert target.path('/v1/messages') == '/anthropic/v1/messages'


@pytest.mark.parametrize('url', [
    'ftp://api.anthropic.com',
    'api.anthropic.com',
    'https://',
    'https://host:notaport',
])
def test_malformed_target_is_rejected_not_defaulted(url):
    with pytest.raises(ValueError):
        UpstreamTarget(url)


# ---------------------------------------------------------------------------
# Client credential extraction
# ---------------------------------------------------------------------------

def test_api_key_header_is_extracted():
    creds = extract_client_credentials({'x-api-key': 'sk-ant-secret'})
    assert creds['auth'] == ('x-api-key', 'sk-ant-secret')


def test_authorization_header_is_extracted():
    creds = extract_client_credentials({'authorization': 'Bearer oauth-token'})
    assert creds['auth'] == ('authorization', 'Bearer oauth-token')


def test_api_key_wins_when_both_headers_are_sent():
    creds = extract_client_credentials(
        {'x-api-key': 'sk-ant-secret', 'authorization': 'Bearer oauth-token'})
    assert creds['auth'] == ('x-api-key', 'sk-ant-secret')


def test_missing_credentials_are_rejected_with_401():
    with pytest.raises(AnthropicRequestError) as exc:
        extract_client_credentials({'content-type': 'application/json'})
    assert exc.value.status_code == 401
    assert exc.value.error_type == 'authentication_error'


def test_client_framing_headers_are_carried_alongside_the_credential():
    creds = extract_client_credentials({
        'x-api-key': 'sk-ant-secret',
        'user-agent': 'claude-cli/2.1.88 (external, cli)',
        'anthropic-version': '2023-06-01',
        'x-app': 'cli',
        'x-unrelated': 'dropped',
    })
    assert creds['forward'] == {
        'user-agent': 'claude-cli/2.1.88 (external, cli)',
        'anthropic-version': '2023-06-01',
        'x-app': 'cli',
    }


# ---------------------------------------------------------------------------
# Outbound headers
# ---------------------------------------------------------------------------

def test_credential_is_forwarded_untouched():
    creds = {'auth': ('authorization', 'Bearer oauth-token'), 'forward': {}}
    headers = _request_headers(creds, betas='', stream=False)
    assert headers['authorization'] == 'Bearer oauth-token'
    assert 'x-api-key' not in headers


def test_no_beta_header_when_the_client_asked_for_none():
    headers = _request_headers({'auth': ('x-api-key', 'k'), 'forward': {}},
                               betas='', stream=False)
    assert 'anthropic-beta' not in headers


def test_client_anthropic_version_overrides_the_default():
    creds = {'auth': ('x-api-key', 'k'), 'forward': {'anthropic-version': '2024-01-01'}}
    headers = _request_headers(creds, betas='', stream=False)
    assert headers['anthropic-version'] == '2024-01-01'


def test_streaming_requests_accept_event_stream():
    creds = {'auth': ('x-api-key', 'k'), 'forward': {}}
    assert _request_headers(creds, '', stream=True)['Accept'] == 'text/event-stream'
    assert _request_headers(creds, '', stream=False)['Accept'] == 'application/json'


# ---------------------------------------------------------------------------
# Body construction
# ---------------------------------------------------------------------------

def _body(payload):
    return json.loads(build_body(payload))


def test_model_alias_is_resolved():
    assert _body({'model': 'sonnet', 'messages': []})['model'] == 'claude-sonnet-4-6'


def test_context_suffix_is_stripped_before_alias_lookup():
    assert _body({'model': 'opus[1m]', 'messages': []})['model'] == 'claude-opus-5'


def test_unknown_model_passes_through_verbatim():
    assert _body({'model': 'claude-future-9', 'messages': []})['model'] == 'claude-future-9'


def test_internal_keys_never_reach_the_wire():
    body = _body({
        'model': 'sonnet',
        'messages': [],
        '_anthropic_beta': ['context-1m-2025-08-07'],
        '_anthproxy_internal_classifier': True,
    })
    assert '_anthropic_beta' not in body
    assert '_anthproxy_internal_classifier' not in body


def test_client_system_prompt_crosses_unmodified():
    system = [{'type': 'text', 'text': 'You are a helpful assistant.'}]
    body = _body({'model': 'sonnet', 'messages': [], 'system': system})
    assert body['system'] == system


def test_absent_system_field_is_not_invented():
    assert 'system' not in _body({'model': 'sonnet', 'messages': []})


def test_no_cache_breakpoint_is_injected_into_tools():
    tools = [{'name': 'read', 'input_schema': {}}]
    assert _body({'model': 'sonnet', 'messages': [], 'tools': tools})['tools'] == tools


def test_haiku_rejects_effort_so_it_is_dropped():
    body = _body({'model': 'haiku', 'messages': [],
                  'output_config': {'effort': 'high', 'other': 1}})
    assert body['output_config'] == {'other': 1}


def test_output_config_disappears_when_effort_was_its_only_key():
    body = _body({'model': 'haiku', 'messages': [], 'output_config': {'effort': 'high'}})
    assert 'output_config' not in body


def test_haiku_drops_adaptive_thinking_but_keeps_manual():
    assert 'thinking' not in _body(
        {'model': 'haiku', 'messages': [], 'thinking': {'type': 'adaptive'}})
    manual = {'type': 'enabled', 'budget_tokens': 1024}
    assert _body({'model': 'haiku', 'messages': [], 'thinking': manual})['thinking'] == manual


def test_fable_drops_explicitly_disabled_thinking():
    assert 'thinking' not in _body(
        {'model': 'fable', 'messages': [], 'thinking': {'type': 'disabled'}})


def test_fixed_sampling_model_drops_sampling_controls():
    body = _body({'model': 'sonnet', 'messages': [], 'temperature': 0.7, 'top_p': 0.9})
    assert 'temperature' not in body and 'top_p' not in body


def test_sampling_controls_survive_on_a_model_that_accepts_them():
    body = _body({'model': 'haiku', 'messages': [], 'temperature': 0.7})
    assert body['temperature'] == 0.7


def test_clear_thinking_edit_is_dropped_when_thinking_is_inactive():
    body = _body({
        'model': 'haiku',
        'messages': [],
        'thinking': {'type': 'adaptive'},
        'context_management': {'edits': [{'type': 'clear_thinking_20251015'}]},
    })
    assert 'context_management' not in body


def test_clear_thinking_edit_survives_when_thinking_is_active():
    edits = [{'type': 'clear_thinking_20251015'}]
    body = _body({
        'model': 'opus',
        'messages': [],
        'thinking': {'type': 'adaptive'},
        'context_management': {'edits': edits},
    })
    assert body['context_management']['edits'] == edits


def test_build_body_does_not_mutate_the_caller_payload():
    payload = {'model': 'haiku', 'messages': [],
               'context_management': {'edits': [{'type': 'clear_thinking_20251015'}]}}
    build_body(payload)
    assert payload['context_management']['edits'] == [{'type': 'clear_thinking_20251015'}]


# ---------------------------------------------------------------------------
# Beta merging
# ---------------------------------------------------------------------------

def test_no_betas_are_added_on_the_clients_behalf():
    assert merge_betas({'model': 'sonnet'}) == ''


def test_client_betas_are_forwarded_and_deduped():
    betas = merge_betas({'model': 'sonnet', '_anthropic_beta': ['a', 'b', 'a']})
    assert betas == 'a,b'


def test_long_context_beta_is_dropped_for_a_non_opus_target():
    payload = {'model': 'haiku', '_anthropic_beta': ['context-1m-2025-08-07']}
    assert merge_betas(payload) == ''


def test_long_context_beta_survives_for_opus():
    payload = {'model': 'opus', '_anthropic_beta': ['context-1m-2025-08-07']}
    assert merge_betas(payload) == 'context-1m-2025-08-07'


def test_clear_thinking_beta_is_dropped_when_thinking_is_inactive():
    payload = {'model': 'haiku', 'thinking': {'type': 'adaptive'},
               '_anthropic_beta': ['clear_thinking_20251015']}
    assert merge_betas(payload) == ''


# ---------------------------------------------------------------------------
# End-to-end against a stub upstream
# ---------------------------------------------------------------------------

class _StubUpstream:
    """A local HTTP server standing in for api.anthropic.com."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                length = int(self.headers.get('content-length') or 0)
                body = json.loads(self.rfile.read(length) or b'{}')
                outer.requests.append({
                    'path': self.path,
                    'headers': self.headers,
                    'body': body,
                })
                status, headers, payload = outer.responder(len(outer.requests), body)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('content-length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'


def _ok(_n, _body):
    payload = json.dumps({'type': 'message', 'stop_reason': 'end_turn'}).encode()
    return 200, {'content-type': 'application/json'}, payload


CREDS = {'auth': ('x-api-key', 'sk-ant-secret'),
         'forward': {'user-agent': 'claude-cli/2.1.88 (external, cli)'}}


def test_request_reaches_upstream_carrying_the_client_credential():
    with _StubUpstream(_ok) as stub:
        transport = AnthropicTransport(stub.base_url)
        result = transport.send_message({'model': 'sonnet', 'messages': []}, CREDS)

    assert result['stop_reason'] == 'end_turn'
    sent = stub.requests[0]
    assert sent['headers']['x-api-key'] == 'sk-ant-secret'
    assert sent['headers']['user-agent'] == 'claude-cli/2.1.88 (external, cli)'
    assert sent['body']['model'] == 'claude-sonnet-4-6'
    assert sent['path'] == '/v1/messages?beta=true'


def test_upstream_401_propagates_without_any_refresh_attempt():
    def unauthorized(_n, _body):
        payload = json.dumps(
            {'error': {'type': 'authentication_error', 'message': 'invalid key'}}).encode()
        return 401, {'content-type': 'application/json'}, payload

    with _StubUpstream(unauthorized) as stub:
        transport = AnthropicTransport(stub.base_url)
        with pytest.raises(AnthropicRequestError) as exc:
            transport.send_message({'model': 'sonnet', 'messages': []}, CREDS)

    assert exc.value.status_code == 401
    assert len(stub.requests) == 1


def test_upstream_400_forwards_its_native_error_type():
    def bad_request(_n, _body):
        payload = json.dumps(
            {'error': {'type': 'invalid_request_error', 'message': 'bad tools'}}).encode()
        return 400, {'content-type': 'application/json'}, payload

    with _StubUpstream(bad_request) as stub:
        with pytest.raises(AnthropicRequestError) as exc:
            AnthropicTransport(stub.base_url).send_message(
                {'model': 'sonnet', 'messages': []}, CREDS)

    assert (exc.value.status_code, exc.value.error_type) == (400, 'invalid_request_error')


def test_retryable_status_is_retried_with_upstream_guidance():
    def flaky(n, _body):
        if n == 1:
            return 503, {'retry-after-ms': '0'}, b'{}'
        return _ok(n, _body)

    with _StubUpstream(flaky) as stub:
        result = AnthropicTransport(stub.base_url).send_message(
            {'model': 'sonnet', 'messages': []}, CREDS)

    assert result['stop_reason'] == 'end_turn'
    assert len(stub.requests) == 2


def test_429_without_timing_guidance_surfaces_immediately():
    def limited(_n, _body):
        payload = json.dumps({'error': {'message': 'slow down'}}).encode()
        return 429, {'content-type': 'application/json'}, payload

    with _StubUpstream(limited) as stub:
        with pytest.raises(AnthropicRequestError) as exc:
            AnthropicTransport(stub.base_url).send_message(
                {'model': 'sonnet', 'messages': []}, CREDS)

    assert exc.value.status_code == 429
    assert len(stub.requests) == 1


def test_thinking_signature_400_recovers_by_stripping_history_once():
    def picky(n, _body):
        if n == 1:
            payload = json.dumps({'error': {
                'type': 'invalid_request_error',
                'message': 'Invalid `signature` in `thinking` block',
            }}).encode()
            return 400, {'content-type': 'application/json'}, payload
        return _ok(n, _body)

    payload = {
        'model': 'haiku',
        'messages': [
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'hm', 'signature': 'opus-minted'},
                {'type': 'text', 'text': 'hello'},
            ]},
            {'role': 'user', 'content': 'go on'},
        ],
    }
    with _StubUpstream(picky) as stub:
        result = AnthropicTransport(stub.base_url).send_message(payload, CREDS)

    assert result['stop_reason'] == 'end_turn'
    assert len(stub.requests) == 2
    retried = stub.requests[1]['body']['messages'][0]['content']
    assert retried == [{'type': 'text', 'text': 'hello'}]


def test_connection_failure_is_flagged_as_a_transport_error(monkeypatch):
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    transport = AnthropicTransport('http://127.0.0.1:1')
    with pytest.raises(AnthropicRequestError) as exc:
        transport.send_message({'model': 'sonnet', 'messages': []}, CREDS)
    assert exc.value.connection_error is True
    assert exc.value.status_code == 502


def test_streaming_yields_whole_sse_events():
    def sse(_n, _body):
        body = (b'event: message_start\ndata: {"type":"message_start"}\n\n'
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        return 200, {'content-type': 'text/event-stream'}, body

    with _StubUpstream(sse) as stub:
        events = list(AnthropicTransport(stub.base_url).send_message_stream(
            {'model': 'sonnet', 'messages': [], 'stream': True}, CREDS))

    assert len(events) == 2
    assert events[0].startswith('event: message_start')
    assert stub.requests[0]['headers']['accept'] == 'text/event-stream'


def test_count_tokens_proxies_the_upstream_answer():
    def counted(_n, _body):
        return 200, {'content-type': 'application/json'}, json.dumps(
            {'input_tokens': 4242}).encode()

    with _StubUpstream(counted) as stub:
        result = AnthropicTransport(stub.base_url).count_tokens(
            {'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'hi'}]}, CREDS)

    assert result['input_tokens'] == 4242
    assert stub.requests[0]['path'] == '/v1/messages/count_tokens?beta=true'


def test_count_tokens_falls_back_to_a_local_estimate():
    def broken(_n, _body):
        return 404, {'content-type': 'application/json'}, b'{}'

    with _StubUpstream(broken) as stub:
        result = AnthropicTransport(stub.base_url).count_tokens(
            {'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'hi' * 100}]}, CREDS)

    assert result['input_tokens'] > 0
    assert result['model'] == 'claude-sonnet-4-6'

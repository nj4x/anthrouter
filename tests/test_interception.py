"""Host interception (ADR-0018): CONNECT tunnels, the api.anthropic.com TLS front,
and where decrypted requests go."""

import email.message
import io
import json
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from anthrouter import interception
from anthrouter.__main__ import main
from anthrouter.config import Config, parse_args, validate_config
from anthrouter.interception import LeafLoadError
from anthrouter.server import create_server


@pytest.fixture(scope='module')
def tls_dir(tmp_path_factory):
    """A throwaway CA and an api.anthropic.com leaf it signed, made the way install.sh makes them."""
    d = tmp_path_factory.mktemp('tls')

    def run(*args):
        subprocess.run(['openssl', *args], cwd=d, check=True, capture_output=True)

    run('ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', 'ca.key')
    run('req', '-x509', '-new', '-key', 'ca.key', '-out', 'ca.pem', '-days', '2',
        '-subj', '/CN=anthrouter test CA',
        '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    run('ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', 'leaf.key')
    run('req', '-new', '-key', 'leaf.key', '-out', 'leaf.csr', '-subj', '/CN=api.anthropic.com')
    (d / 'leaf.ext').write_text('subjectAltName=DNS:api.anthropic.com\n'
                                'extendedKeyUsage=serverAuth\n'
                                'basicConstraints=CA:FALSE\n')
    run('x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
        '-CAcreateserial', '-out', 'leaf.pem', '-days', '2', '-extfile', 'leaf.ext')
    return d


class _Recorder(BaseHTTPRequestHandler):
    """A plain-HTTP far end that records each request and answers 200 JSON."""

    received: list = []
    status = 200

    def _answer(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
        type(self).received.append({
            'method': self.command, 'path': self.path, 'body': body,
            'headers': {k.lower(): v for k, v in self.headers.items()},
        })
        data = json.dumps({'far_end': True}).encode()
        self.send_response(type(self).status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Far-End', 'yes')
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def far_end():
    _Recorder.received = []
    _Recorder.status = 200
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.url = f'http://127.0.0.1:{server.server_address[1]}'
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def echo_port():
    """A TCP server that echoes every byte back, standing in for an opaque-tunnel host."""
    listener = socket.create_server(('127.0.0.1', 0))

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                while data := conn.recv(4096):
                    conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    yield listener.getsockname()[1]
    listener.close()


def _unused_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _connect(server, authority: str) -> tuple[socket.socket, bytes]:
    """Send CONNECT and return the socket plus the proxy's raw response head."""
    sock = socket.create_connection(server.server_address, timeout=5)
    sock.sendall(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode())
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    return sock, head


def _tls(server) -> Path:
    return Path(server.RequestHandlerClass.config.interception_tls_dir)


def _read_all(sock) -> bytes:
    out = b''
    while chunk := sock.recv(4096):
        out += chunk
    return out


def _decrypted(server, tls_dir, request: bytes) -> bytes:
    """CONNECT to api.anthropic.com, complete TLS against the test CA, send *request*."""
    sock, head = _connect(server, f'api.anthropic.com:{interception.CONNECT_PORT}')
    assert head.startswith(b'HTTP/1.0 200')
    ctx = interception._StdlibSSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(tls_dir / 'ca.pem')
    with ctx.wrap_socket(sock, server_hostname='api.anthropic.com') as tls:
        assert tls.selected_alpn_protocol() in (None, 'http/1.1')
        tls.sendall(request)
        return _read_all(tls)


@pytest.fixture
def interceptor(proxy, tls_dir, monkeypatch, far_end):
    """A proxy with connect_proxy on; port 443 and the real upstream are redirected locally."""
    def _build(**overrides):
        monkeypatch.setattr(interception, 'INTERCEPT_UPSTREAM', far_end.url)
        return proxy(connect_proxy=True, interception_tls_dir=str(tls_dir), **overrides)
    return _build


def _messages_request(path='/v1/messages/count_tokens?beta=true', key=b'sk-test') -> bytes:
    body = json.dumps({'model': 'claude-sonnet-5', 'messages': [
        {'role': 'user', 'content': 'hi'}]}).encode()
    return (f'POST {path} HTTP/1.1\r\nHost: api.anthropic.com\r\n'
            f'Content-Type: application/json\r\nx-api-key: {key.decode()}\r\n'
            f'Connection: keep-alive\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body


# -- config -------------------------------------------------------------------

def test_connect_proxy_refuses_a_non_loopback_host():
    errors = validate_config(Config(connect_proxy=True, host='0.0.0.0'))
    assert any('connect_proxy requires a loopback host' in e for e in errors)


def test_connect_proxy_accepts_loopback_hosts():
    for host in ('127.0.0.1', '::1', 'localhost'):
        assert validate_config(Config(connect_proxy=True, host=host)) == []


def test_negative_tunnel_idle_timeout_is_rejected():
    assert any('tunnel_idle_timeout' in e for e in validate_config(Config(tunnel_idle_timeout=-1)))


def test_interception_target_must_be_an_http_url():
    assert any('interception_target' in e
               for e in validate_config(Config(interception_target='ftp://x')))
    assert validate_config(Config(interception_target='http://127.0.0.1:8787/w/claude')) == []


def test_tls_dir_defaults_under_the_anthrouter_home(tmp_path):
    cfg = parse_args(['--anthrouter-home', str(tmp_path)])
    assert cfg.interception_tls_dir == str(tmp_path / 'tls')
    assert cfg.connect_proxy is False


def test_interception_env_vars_are_read(monkeypatch):
    monkeypatch.setenv('ANTHROUTER_CONNECT_PROXY', '1')
    monkeypatch.setenv('ANTHROUTER_INTERCEPTION_TARGET', 'http://127.0.0.1:8787/w/claude/')
    monkeypatch.setenv('ANTHROUTER_TUNNEL_IDLE_TIMEOUT', '30')
    cfg = parse_args([])
    assert (cfg.connect_proxy, cfg.interception_target, cfg.tunnel_idle_timeout) == (
        True, 'http://127.0.0.1:8787/w/claude', 30)


# -- CONNECT parsing ----------------------------------------------------------

@pytest.mark.parametrize('authority, expected', [
    ('api.anthropic.com:443', ('api.anthropic.com', 443)),
    ('[::1]:443', ('::1', 443)),
    ('host', None),
    ('host:', None),
    ('host:abc', None),
    ('[::1]443', None),
    ('host:\u00b9', None),
    ('host:0', None),
    ('host:65536', None),
    ('api.anth\xadropic.com:443', None),
    ('\xb927.0.0.1:443', None),
    ('127.0.0.1\xad:443', None),
])
def test_parse_connect_target(authority, expected):
    assert interception.parse_connect_target(authority) == expected


@pytest.mark.parametrize('host', ['160.79.104.10', '::1', '127.1', '2130706433', '0x7f000001'])
def test_every_resolver_address_form_counts_as_an_ip_literal(host):
    assert interception.is_ip_literal(host)


def test_host_names_are_not_ip_literals():
    assert not interception.is_ip_literal('api.anthropic.com')


def test_interception_target_with_a_bad_port_is_rejected():
    assert any('interception_target' in e
               for e in validate_config(Config(interception_target='http://h:abc')))


class _BodyOnly:
    """Just the handler surface read_request_body touches."""

    def __init__(self, headers: dict, raw: bytes):
        self.headers = email.message.Message()
        for name, value in headers.items():
            self.headers[name] = value
        self.rfile = io.BytesIO(raw)


@pytest.mark.parametrize('headers, raw', [
    ({'Transfer-Encoding': 'chunked'}, b'5\r\nab'),
    ({'Transfer-Encoding': 'chunked'}, b''),
    ({'Transfer-Encoding': 'chunked'}, b'3\r\nabc\r\n0\r\n'),
    ({'Content-Length': '5'}, b'ab'),
])
def test_truncated_bodies_raise(headers, raw):
    with pytest.raises(ValueError):
        interception.read_request_body(_BodyOnly(headers, raw))


def test_host_match_ignores_case_and_one_trailing_dot():
    assert interception.normalize_host('API.Anthropic.COM.') == 'api.anthropic.com'


# -- CONNECT policy -----------------------------------------------------------

def test_connect_is_refused_when_the_flag_is_off(proxy):
    sock, head = _connect(proxy(), 'example.com:443')
    sock.close()
    assert head.startswith(b'HTTP/1.0 405')


@pytest.mark.parametrize('authority', ['example.com:8443', '160.79.104.10:443', '[::1]:443'])
def test_connect_refuses_other_ports_and_ip_literals(interceptor, authority):
    sock, head = _connect(interceptor(), authority)
    sock.close()
    assert head.startswith(b'HTTP/1.0 403')


def test_startup_aborts_when_the_leaf_is_missing(tmp_path):
    with pytest.raises(LeafLoadError):
        create_server(Config(port=0, connect_proxy=True, interception_tls_dir=str(tmp_path)))


def test_main_returns_1_when_the_leaf_is_missing(tmp_path):
    assert main(['--port', '0', '--connect-proxy', '--no-tls-system-trust',
                 '--interception-tls-dir', str(tmp_path), '--log-file', '']) == 1


# -- opaque tunnels -----------------------------------------------------------

def test_other_hosts_get_an_opaque_tunnel(interceptor, monkeypatch, echo_port):
    monkeypatch.setattr(interception, 'CONNECT_PORT', echo_port)
    sock, head = _connect(interceptor(), f'localhost:{echo_port}')
    with sock:
        assert head.startswith(b'HTTP/1.0 200 Connection Established')
        sock.sendall(b'opaque bytes')
        assert sock.recv(4096) == b'opaque bytes'


def test_idle_tunnel_is_closed(interceptor, monkeypatch, echo_port):
    monkeypatch.setattr(interception, 'CONNECT_PORT', echo_port)
    sock, head = _connect(interceptor(tunnel_idle_timeout=1), f'localhost:{echo_port}')
    with sock:
        assert head.startswith(b'HTTP/1.0 200')
        assert sock.recv(4096) == b''


def test_unreachable_tunnel_host_is_a_tagged_502(interceptor, monkeypatch):
    port = _unused_port()
    monkeypatch.setattr(interception, 'CONNECT_PORT', port)
    sock, head = _connect(interceptor(), f'localhost:{port}')
    sock.close()
    assert head.startswith(b'HTTP/1.0 502')
    assert b'X-Anthrouter-Error: upstream-connect' in head


def test_bytes_sent_right_after_connect_reach_the_tunnel(interceptor, monkeypatch, echo_port):
    monkeypatch.setattr(interception, 'CONNECT_PORT', echo_port)
    server = interceptor()
    sock = socket.create_connection(server.server_address, timeout=5)
    with sock:
        sock.sendall(f'CONNECT localhost:{echo_port} HTTP/1.1\r\n\r\n'.encode() + b'early')
        data = b''
        while b'early' not in data:
            chunk = sock.recv(4096)
            assert chunk, f'proxy closed before echoing: {data!r}'
            data += chunk
        assert data.startswith(b'HTTP/1.0 200')


# -- decrypted api.anthropic.com ----------------------------------------------

def test_messages_endpoint_goes_to_the_interception_target(interceptor, far_end, monkeypatch):
    target = far_end.url + '/w/claude'
    monkeypatch.setattr(interception, 'INTERCEPT_UPSTREAM', 'http://127.0.0.1:1')
    server = interceptor(interception_target=target)
    reply = _decrypted(server, _tls(server), _messages_request())
    assert reply.startswith(b'HTTP/1.0 200')
    assert b'X-Far-End: yes' in reply
    [req] = _Recorder.received
    assert req['path'] == '/w/claude/v1/messages/count_tokens?beta=true'
    assert req['headers']['host'] == f'127.0.0.1:{far_end.server_address[1]}'
    assert req['headers']['x-api-key'] == 'sk-test'
    assert 'connection' not in req['headers'] or req['headers']['connection'] != 'keep-alive'
    assert json.loads(req['body'])['model'] == 'claude-sonnet-5'


def test_unreachable_interception_target_is_a_tagged_502(interceptor):
    server = interceptor(interception_target=f'http://127.0.0.1:{_unused_port()}')
    reply = _decrypted(server, _tls(server), _messages_request())
    assert reply.startswith(b'HTTP/1.0 502')
    assert b'X-Anthrouter-Error: interception-target-unreachable' in reply


def test_messages_are_handled_in_process_without_a_target(interceptor, upstream):
    server = interceptor()
    reply = _decrypted(server, _tls(server), _messages_request())
    assert reply.startswith(b'HTTP/1.0 200')
    assert [r['path'] for r in upstream.RequestHandlerClass.received] == [
        '/v1/messages/count_tokens?beta=true']


def test_in_process_messages_still_require_a_credential(interceptor):
    server = interceptor()
    request = _messages_request(path='/v1/messages').replace(b'x-api-key: sk-test\r\n', b'')
    reply = _decrypted(server, _tls(server), request)
    assert reply.startswith(b'HTTP/1.0 401')


@pytest.mark.parametrize('request_line', [
    b'GET /v1/models HTTP/1.1',
    b'POST /v1/messages/batches HTTP/1.1',
    b'DELETE /api/oauth/thing HTTP/1.1',
])
def test_other_paths_pass_through_to_anthropic_unchanged(interceptor, far_end, request_line):
    server = interceptor(interception_target='http://127.0.0.1:1')
    request = (request_line + b'\r\nHost: api.anthropic.com\r\n'
               b'Authorization: Bearer tok\r\nContent-Length: 2\r\n\r\n{}')
    reply = _decrypted(server, _tls(server), request)
    assert reply.startswith(b'HTTP/1.0 200')
    assert reply.endswith(b'{"far_end": true}')
    [req] = _Recorder.received
    assert (req['method'] + ' ' + req['path']).encode() == request_line.rsplit(b' ', 1)[0]
    assert req['headers']['authorization'] == 'Bearer tok'
    assert req['body'] == b'{}'


def test_unreachable_anthropic_is_a_tagged_502(interceptor, monkeypatch):
    server = interceptor()
    monkeypatch.setattr(interception, 'INTERCEPT_UPSTREAM', f'http://127.0.0.1:{_unused_port()}')
    reply = _decrypted(server, _tls(server), b'GET /v1/models HTTP/1.1\r\nHost: x\r\n\r\n')
    assert reply.startswith(b'HTTP/1.0 502')
    assert b'X-Anthrouter-Error: upstream-connect' in reply


def test_chunked_request_bodies_are_forwarded_with_a_length(interceptor):
    server = interceptor()
    request = (b'POST /v1/files HTTP/1.1\r\nHost: api.anthropic.com\r\n'
               b'Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n')
    reply = _decrypted(server, _tls(server), request)
    assert reply.startswith(b'HTTP/1.0 200')
    [req] = _Recorder.received
    assert req['body'] == b'abcde'
    assert req['headers']['content-length'] == '5'
    assert 'transfer-encoding' not in req['headers']


@pytest.mark.parametrize('framing', [
    b'Content-Length: -1\r\n\r\n',
    b'Content-Length: abc\r\n\r\n',
    b'Content-Length: +5\r\n\r\nabcde',
    b'Content-Length: 5_0\r\n\r\nabcde',
    b'Transfer-Encoding: chunked\r\n\r\n-1\r\nabc\r\n0\r\n\r\n',
    b'Transfer-Encoding: chunked\r\n\r\nzz\r\n',
    b'Transfer-Encoding: chunked\r\n\r\n0x5\r\nabcde\r\n0\r\n\r\n',
])
def test_malformed_body_length_is_a_400(interceptor, framing):
    server = interceptor()
    request = b'POST /v1/files HTTP/1.1\r\nHost: api.anthropic.com\r\n' + framing
    reply = _decrypted(server, _tls(server), request)
    assert reply.startswith(b'HTTP/1.0 400')
    assert _Recorder.received == []


def test_mixed_case_connect_host_is_decrypted(interceptor, far_end):
    server = interceptor()
    sock, head = _connect(server, f'API.Anthropic.COM.:{interception.CONNECT_PORT}')
    assert head.startswith(b'HTTP/1.0 200')
    ctx = interception._StdlibSSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(_tls(server) / 'ca.pem')
    with ctx.wrap_socket(sock, server_hostname='api.anthropic.com') as tls:
        tls.sendall(b'GET /v1/models HTTP/1.1\r\nHost: x\r\n\r\n')
        assert _read_all(tls).startswith(b'HTTP/1.0 200')
    assert [r['headers']['host'] for r in _Recorder.received] == [
        f'127.0.0.1:{far_end.server_address[1]}']


def test_connect_inside_the_decrypted_leg_is_refused(interceptor):
    server = interceptor()
    reply = _decrypted(server, _tls(server), b'CONNECT example.com:443 HTTP/1.1\r\n\r\n')
    assert reply.startswith(b'HTTP/1.0 405')


@pytest.fixture
def truststore_injected():
    """The process state __main__ leaves after ADR-0009's injection, undone afterwards."""
    truststore = pytest.importorskip('truststore')
    truststore.inject_into_ssl()
    yield
    truststore.extract_from_ssl()


def test_interception_works_after_truststore_injection(truststore_injected, interceptor):
    server = interceptor()
    reply = _decrypted(server, _tls(server), b'GET /v1/models HTTP/1.1\r\nHost: x\r\n\r\n')
    assert reply.startswith(b'HTTP/1.0 200')

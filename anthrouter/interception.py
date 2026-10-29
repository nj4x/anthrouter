"""Host interception (ADR-0018): CONNECT parsing, opaque tunnels, the TLS front
for ``api.anthropic.com``, and raw forwarding for the decrypted leg.

Nothing here routes, sanitizes or records. The decrypted Messages endpoints
reach that logic through the ordinary handler path. Tunnels move bytes
unchanged; forwarded requests change only ``Host``, hop-by-hop headers and
chunked framing.
"""

import http.client
import ipaddress
import logging
import re
import selectors
import socket
import ssl
from pathlib import Path

from .http_util import DEFAULT_TIMEOUT, UpstreamTarget
from .mapper import anthropic_error_payload

logger = logging.getLogger(__name__)

# Captured at import, before __main__ runs truststore.inject_into_ssl() (ADR-0009).
# truststore's class is built for client-side verification against the OS store;
# the server-side context only presents the leaf and never verifies a peer.
_StdlibSSLContext = ssl.SSLContext

INTERCEPT_HOST = 'api.anthropic.com'
INTERCEPT_UPSTREAM = f'https://{INTERCEPT_HOST}'
CONNECT_PORT = 443
CONNECT_TIMEOUT = 10
ERROR_HEADER = 'X-Anthrouter-Error'
TARGET_UNREACHABLE = 'interception-target-unreachable'
UPSTREAM_CONNECT = 'upstream-connect'

_BUFFER = 64 * 1024
# ASCII only: getaddrinfo IDNA-folds other characters (a soft hyphen vanishes),
# so 'api.anth\xadropic.com' would dial the real host past the decrypt match.
_HOST_CHARS = re.compile(r'[A-Za-z0-9._%:-]+')
_HEX = re.compile(rb'[0-9A-Fa-f]+')
_DECIMAL = re.compile(r'[0-9]+')
# The Messages API's own request limit, applied to every forwarded hop (passthrough
# uploads included); a larger body is refused before it is buffered.
MAX_BODY = 32 * 1024 * 1024
# Framing lines (chunk sizes, the CRLF after a chunk, trailers) are short; a
# bound keeps a line with no newline from buffering past MAX_BODY.
_MAX_LINE = 1024
_HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade',
})


class LeafLoadError(RuntimeError):
    """The interception leaf or its key could not be loaded."""


def build_server_context(tls_dir: str) -> ssl.SSLContext:
    """Load the ``api.anthropic.com`` leaf into a server-side context.

    Raises ``LeafLoadError`` when the files are missing or do not match; the
    caller aborts startup rather than tunnel the host unseen.
    """
    # No property setters (minimum_version etc.): after truststore's injection the
    # stdlib setters resolve the patched global ssl.SSLContext and recurse. The
    # protocol's own default minimum is already TLS 1.2.
    ctx = _StdlibSSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(Path(tls_dir) / 'leaf.pem', Path(tls_dir) / 'leaf.key')
    except (OSError, ssl.SSLError) as exc:
        raise LeafLoadError(f'cannot load leaf.pem/leaf.key from {tls_dir}: {exc}') from exc
    ctx.set_alpn_protocols(['http/1.1'])
    return ctx


def parse_connect_target(authority: str) -> tuple[str, int] | None:
    """Split a CONNECT ``host:port`` (or ``[v6]:port``); ``None`` when malformed."""
    if authority.startswith('['):
        host, sep, rest = authority[1:].partition(']')
        if not sep or not rest.startswith(':'):
            return None
        port_text = rest[1:]
    else:
        host, sep, port_text = authority.rpartition(':')
        if not sep:
            return None
    if not _HOST_CHARS.fullmatch(host) or not _DECIMAL.fullmatch(port_text):
        return None
    # Empty labels ('api.anthropic.com..', '.api') survive the one-dot strip in
    # normalize_host and resolve past the decrypt match; labels over 63
    # characters fail later in IDNA encoding. IP literals keep their own form.
    if not is_ip_literal(host):
        labels = host[:-1].split('.') if host.endswith('.') else host.split('.')
        if not all(0 < len(label) <= 63 for label in labels):
            return None
    port = int(port_text)
    return (host, port) if 0 < port < 65536 else None


def normalize_host(host: str) -> str:
    """Lower-case and drop one trailing dot, so ``API.Anthropic.com.`` still matches."""
    host = host.lower()
    return host[:-1] if host.endswith('.') else host


def is_ip_literal(host: str) -> bool:
    """True for any address form ``getaddrinfo`` would accept, ``127.1`` and ``2130706433`` included."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


def take_buffered(rfile, sock: socket.socket) -> bytes:
    """Return bytes the client sent after the CONNECT line that ``rfile`` already buffered.

    The socket goes non-blocking only for the peek, so an empty buffer returns
    ``b''`` instead of waiting on the client.
    """
    timeout = sock.gettimeout()
    sock.setblocking(False)
    try:
        pending = rfile.peek()
    finally:
        sock.settimeout(timeout)
    return rfile.read(len(pending)) if pending else b''


def splice(client: socket.socket, upstream: socket.socket, idle_timeout: int) -> str:
    """Copy bytes both ways until a side closes, errors, or nothing moves for *idle_timeout* s.

    Returns the reason the tunnel ended: ``closed``, ``idle`` or ``error``.
    """
    # A peer that stops reading would otherwise pin sendall() past the idle timeout.
    for sock in (client, upstream):
        sock.settimeout(idle_timeout or None)
    sel = selectors.DefaultSelector()
    sel.register(client, selectors.EVENT_READ, upstream)
    sel.register(upstream, selectors.EVENT_READ, client)
    try:
        while True:
            events = sel.select(idle_timeout or None)
            if not events:
                return 'idle'
            for key, _ in events:
                data = key.fileobj.recv(_BUFFER)
                if not data:
                    return 'closed'
                key.data.sendall(data)
    except OSError:
        return 'error'
    finally:
        sel.close()


def _framing_line(rfile) -> bytes:
    line = rfile.readline(_MAX_LINE)
    if not line.endswith(b'\n'):
        raise ValueError('framing line too long or body cut off')
    return line


def _check_size(total: int) -> None:
    if total > MAX_BODY:
        raise ValueError(f'body over {MAX_BODY} bytes')


def read_request_body(handler) -> bytes:
    """Read the request body, de-chunking it so the forwarded request carries a length.

    Raises ``ValueError`` on malformed framing, a body over ``MAX_BODY``, or a
    ``Transfer-Encoding`` other than plain ``chunked``: the header is hop-by-hop
    and dropped on forward, so any other coding would be lost.
    """
    codings = [c.strip().lower()
               for value in handler.headers.get_all('Transfer-Encoding', [])
               for c in value.split(',') if c.strip()]
    if codings and codings != ['chunked']:
        raise ValueError(f'unsupported Transfer-Encoding {", ".join(codings)!r}')
    if codings:
        body, total = bytearray(), 0
        while True:
            size_text = _framing_line(handler.rfile).split(b';', 1)[0].strip()
            if not _HEX.fullmatch(size_text):
                raise ValueError(f'bad chunk size {size_text[:16]!r}')
            size = int(size_text, 16)
            total += size
            _check_size(total)
            if size == 0:
                while _framing_line(handler.rfile) not in (b'\r\n', b'\n'):
                    pass
                return bytes(body)
            chunk = handler.rfile.read(size)
            if len(chunk) != size or _framing_line(handler.rfile) not in (b'\r\n', b'\n'):
                raise ValueError('body ended inside a chunk')
            body += chunk
    length_text = handler.headers.get('Content-Length', '').strip() or '0'
    if not _DECIMAL.fullmatch(length_text):
        raise ValueError(f'bad Content-Length {length_text[:16]!r}')
    length = int(length_text)
    _check_size(length)
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ValueError('body shorter than Content-Length')
    return body


def _forwardable(headers) -> list[tuple[str, str]]:
    named = {
        token.strip().lower()
        for value in headers.get_all('Connection', [])
        for token in value.split(',')
    }
    return [
        (name, value) for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP
        and name.lower() not in named
        and name.lower() not in ('host', 'content-length')
    ]


def forward(handler, base_url: str, error_code: str) -> None:
    """Send the handler's current request to *base_url* + its path and stream the reply back.

    A failed connect answers ``502`` with ``X-Anthrouter-Error: <error_code>``,
    so a caller can tell it from a ``502`` the far end sent. Any status the far
    end does send passes back unchanged.
    """
    target = UpstreamTarget(base_url)
    try:
        body = read_request_body(handler)
    except ValueError as exc:
        handler.close_connection = True
        handler._send_json(400, anthropic_error_payload(
            'invalid_request_error', f'Malformed request body: {exc}'))
        return
    conn = target.connect(timeout=CONNECT_TIMEOUT)
    try:
        conn.connect()
    except OSError as exc:
        logger.warning('%s %s connect to %s failed: %s',
                       handler._log_tag(), error_code, target.netloc, exc)
        handler._send_json(
            502, anthropic_error_payload('api_error', f'Could not reach {target.netloc}'),
            extra_headers={ERROR_HEADER: error_code},
        )
        return
    try:
        conn.sock.settimeout(DEFAULT_TIMEOUT)
        conn.putrequest(handler.command, target.path(handler.path),
                        skip_host=True, skip_accept_encoding=True)
        conn.putheader('Host', target.netloc)
        for name, value in _forwardable(handler.headers):
            conn.putheader(name, value)
        if body or handler.command in ('POST', 'PUT', 'PATCH'):
            conn.putheader('Content-Length', str(len(body)))
        conn.endheaders(body or None)
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        logger.warning('%s forwarded request to %s failed before a response: %s',
                       handler._log_tag(), target.netloc, exc)
        handler._send_json(502, anthropic_error_payload('api_error', 'Upstream request failed'))
        conn.close()
        handler.close_connection = True
        return
    try:
        _relay_response(handler, resp)
    except (BrokenPipeError, ConnectionResetError):
        logger.info('%s client disconnected during forwarded response', handler._log_tag())
    except (OSError, http.client.HTTPException) as exc:
        logger.warning('%s forwarded response from %s broke off: %s',
                       handler._log_tag(), target.netloc, exc)
    finally:
        conn.close()
        handler.close_connection = True


def _relay_response(handler, resp) -> None:
    handler.send_response_only(resp.status, resp.reason)
    handler.log_request(resp.status)
    chunked = resp.chunked
    for name, value in resp.getheaders():
        lowered = name.lower()
        if lowered in _HOP_BY_HOP or (chunked and lowered == 'content-length'):
            continue
        handler.send_header(name, value)
    handler.send_header('Connection', 'close')
    handler.end_headers()
    if handler.command == 'HEAD':
        return
    while True:
        chunk = resp.read1(_BUFFER)
        if not chunk:
            break
        handler.wfile.write(chunk)
        handler.wfile.flush()

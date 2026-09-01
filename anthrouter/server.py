"""Server construction: build the collaborators once, bind them to the handler."""

from __future__ import annotations

import logging
from http.server import ThreadingHTTPServer

from .config import Config
from .db import RequestDB
from .handlers import ProxyRequestHandler
from .oauth_usage import OAuthUsageCache
from .session_state import SessionState
from .transport import AnthropicTransport

logger = logging.getLogger(__name__)

_LOOPBACK = ('127.0.0.1', 'localhost', '::1', '')


def make_handler_class(config: Config, transport, sessions, request_db=None, oauth_cache=None):
    class Handler(ProxyRequestHandler):
        pass

    Handler.config = config
    Handler.transport = transport
    Handler.sessions = sessions
    Handler.request_db = request_db
    Handler.oauth_cache = oauth_cache
    return Handler


def create_server(config: Config) -> ThreadingHTTPServer:
    """Build the transport, session state, and optional DB, then bind the server.

    ``UpstreamTarget`` validation happens here rather than at first dispatch, so
    a malformed ``--upstream-base-url`` fails at boot instead of on a live
    request carrying the client's credential.
    """
    transport = AnthropicTransport(config.upstream_base_url)
    sessions = SessionState(auto_model_routing=config.auto_model_routing)
    request_db = None
    if config.db_path:
        request_db = RequestDB(config.db_path, retention_days=config.db_retention_days)
        logger.info('Recording requests to %s (retention: %s)', config.db_path,
                    f'{config.db_retention_days} days' if config.db_retention_days
                    else 'forever')
    oauth_cache = OAuthUsageCache()

    handler_class = make_handler_class(config, transport, sessions, request_db, oauth_cache)
    server = ThreadingHTTPServer((config.host, config.port), handler_class)
    if config.enable_ui and config.host not in _LOOPBACK:
        logger.warning(
            'SECURITY: --enable-ui is active and the server is bound to %s. The '
            'admin UI has no authentication — any host on this network can read '
            'conversation history. Bind to 127.0.0.1 for local-only access.',
            config.host,
        )
    return server

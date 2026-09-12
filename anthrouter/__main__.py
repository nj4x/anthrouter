import logging
import sys
from pathlib import Path

from .config import parse_args
from .server import create_server


def _setup_logging(level: str, log_file: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=getattr(logging, level),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=handlers,
    )


def _maybe_inject_system_trust(cfg, logger: logging.Logger) -> bool:
    """Delegate TLS verification to the OS trust store, if requested.

    Must run before any ssl.SSLContext is created (before create_server()):
    truststore.inject_into_ssl() patches the ssl module process-wide, so every
    client built afterwards - main passthrough, classifier, oauth usage meter -
    picks it up. Returns False (caller should abort startup) if the flag is set
    but truststore isn't installed, per the fail-closed convention: silently
    falling back to the (already broken) default verification would just
    reproduce CERTIFICATE_VERIFY_FAILED downstream instead of at startup.
    """
    if not cfg.tls_system_trust:
        return True
    try:
        import truststore
    except ImportError:
        logger.error(
            '--tls-system-trust set but truststore is not installed; run: '
            'pip install truststore'
        )
        return False
    truststore.inject_into_ssl()
    logger.info('TLS verification delegated to the OS trust store (truststore)')
    return True


def main(argv=None) -> int:
    cfg = parse_args(argv)
    _setup_logging(cfg.log_level, cfg.log_file)
    logger = logging.getLogger(__name__)

    if not _maybe_inject_system_trust(cfg, logger):
        return 1

    if cfg.db_path:
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)

    server = create_server(cfg)
    logger.info('anthrouter listening on %s:%d, forwarding to %s',
                cfg.host, cfg.port, cfg.upstream_base_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Shutting down')
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

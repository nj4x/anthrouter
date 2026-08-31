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


def main(argv=None) -> int:
    cfg = parse_args(argv)
    _setup_logging(cfg.log_level, cfg.log_file)
    logger = logging.getLogger(__name__)

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

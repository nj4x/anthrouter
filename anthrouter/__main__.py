import dataclasses
import json
import logging
import sys

from .config import parse_args


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
    print(json.dumps(dataclasses.asdict(cfg), indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

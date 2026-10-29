"""Replay stored classifier inputs through the current classifier prompt.

Manual pre-release check for ADR-0021. Not run in CI.

Reads ``classifier_summary_json`` rows from the anthrouter DB (read-only),
sends each one to the Anthropic Messages API with the current
``_CLASSIFIER_SYSTEM`` prompt, and prints the new score next to the stored
score. Requires ``ANTHROPIC_API_KEY`` in the environment.

Usage:
    ANTHROPIC_API_KEY=... python scripts/replay_classifier.py [--limit 50] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

from anthrouter.model_router import (
    _CLASSIFIER_MAX_TOKENS,
    _CLASSIFIER_SYSTEM,
    parse_classifier_score,
)

API_URL = 'https://api.anthropic.com/v1/messages'
DEFAULT_DB = Path.home() / '.anthrouter' / 'anthrouter.db'
DEFAULT_MODEL = 'claude-haiku-5-5'


def _call(api_key: str, model: str, user_text: str) -> dict:
    body = json.dumps({
        'model': model,
        'max_tokens': _CLASSIFIER_MAX_TOKENS,
        'system': _CLASSIFIER_SYSTEM,
        'messages': [{'role': 'user', 'content': user_text}],
    }).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    args = parser.parse_args()

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print('ANTHROPIC_API_KEY not set', file=sys.stderr)
        return 2

    conn = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    rows = conn.execute(
        'SELECT id, user_prompt_text, classifier_summary_json, classifier_raw_response '
        'FROM requests '
        "WHERE classifier_summary_json LIKE '%prior_response_summary%' "
        'ORDER BY id DESC LIMIT ?',
        (args.limit,),
    ).fetchall()

    print(f'{"id":>7} {"stored":>6} {"new":>4} {"delta":>5}  prompt')
    for row_id, prompt, summary_json, stored in rows:
        try:
            response = _call(api_key, args.model, summary_json)
        except Exception as exc:  # network or auth failure: report, keep going
            print(f'{row_id:>7} {stored!s:>6} ERR  {type(exc).__name__}: {exc}')
            continue
        new = parse_classifier_score(response)
        delta = '' if new is None or stored is None else f'{new - int(stored):+d}'
        preview = (prompt or '')[:60].replace('\n', ' ')
        print(f'{row_id:>7} {stored!s:>6} {new!s:>4} {delta:>5}  {preview}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

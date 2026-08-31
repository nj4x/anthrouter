"""SQLite persistence for the anthrouter observability UI.

Schema version: 1 (single migration; this schema is not ported from anthproxy's
13-migration history).  Three tables:

``requests``
    One row per dispatched request, with the routing decision folded in.
``prompt_store``
    Deduped system / sanitized-system / tools content, ref-counted so retention
    can drop entries once no request references them.
``sanitizer_events``
    Volatile system blocks stripped or flagged for one request (1:N).

Full-text search is an FTS5 external-content index over the user and response
text with the ``trigram`` tokenizer, so substring queries work without a
casefolded shadow column.

Thread safety: one write connection guarded by a lock; per-thread read
connections over WAL.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .model_config import MODEL_PRICING
from .model_tier import classify_model_tier

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_RETENTION_INTERVAL_SECS = 24 * 3600
_RETENTION_CHUNK = 1000

_NOW = "(strftime('%Y-%m-%dT%H:%M:%fZ','now'))"


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def _apply_migration_0(conn: sqlite3.Connection) -> None:
    """Create the whole schema (version 0 -> 1)."""
    statements = [
        f"""CREATE TABLE IF NOT EXISTS requests (
            id                          INTEGER PRIMARY KEY,
            session_id                  TEXT    NOT NULL,
            request_ts                  TEXT    NOT NULL DEFAULT {_NOW},
            requested_model             TEXT    NOT NULL,
            routed_model                TEXT,
            classification              TEXT    CHECK(classification IN ('trivial','standard','deep') OR classification IS NULL),
            reason_code                 TEXT,
            estimated_input_tokens      INTEGER,
            input_tokens                INTEGER,
            output_tokens               INTEGER,
            cache_creation_tokens       INTEGER,
            cache_read_tokens           INTEGER,
            duration_ms                 INTEGER,
            status                      TEXT    NOT NULL CHECK(status IN ('success','error','rate_limited')),
            error                       TEXT,
            applied                     INTEGER,
            cost_estimate               REAL,
            net_savings_usd             REAL,
            classifier_overhead_usd     REAL,
            cache_savings_usd           REAL,
            model_tier                  TEXT    CHECK(model_tier IN ('haiku','sonnet','opus','fable') OR model_tier IS NULL),
            attempt                     INTEGER NOT NULL DEFAULT 1,
            classifier_model            TEXT,
            classifier_summary_json     TEXT,
            classifier_raw_response     TEXT,
            classifier_format           TEXT,
            system_prompt_sha256        TEXT,
            system_prompt_sanitized_sha256 TEXT,
            tools_sha256                TEXT,
            user_prompt_text            TEXT,
            response_text               TEXT,
            ratelimit_requests_remaining        INTEGER,
            ratelimit_tokens_remaining          INTEGER,
            ratelimit_input_tokens_remaining    INTEGER,
            ratelimit_output_tokens_remaining   INTEGER,
            ratelimit_reset_at          TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS ix_req_session ON requests(session_id)",
        "CREATE INDEX IF NOT EXISTS ix_req_ts ON requests(request_ts DESC)",
        "CREATE INDEX IF NOT EXISTS ix_req_session_ts ON requests(session_id, request_ts DESC)",
        "CREATE INDEX IF NOT EXISTS ix_req_model_tier_ts ON requests(model_tier, request_ts DESC)",
        "CREATE INDEX IF NOT EXISTS ix_req_ts_id ON requests(request_ts, id)",

        f"""CREATE TABLE IF NOT EXISTS prompt_store (
            content_hash  TEXT    PRIMARY KEY,
            content_type  TEXT    NOT NULL CHECK(content_type IN ('system','system_sanitized','tools')),
            content       TEXT    NOT NULL,
            char_count    INTEGER NOT NULL,
            ref_count     INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT    NOT NULL DEFAULT {_NOW}
        )""",
        "CREATE INDEX IF NOT EXISTS ix_prompt_refcount ON prompt_store(ref_count, first_seen_at)",

        f"""CREATE TABLE IF NOT EXISTS sanitizer_events (
            id              INTEGER PRIMARY KEY,
            request_id      INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
            event_ts        TEXT    NOT NULL DEFAULT {_NOW},
            block_type      TEXT    NOT NULL,
            is_allowlisted  INTEGER NOT NULL,
            payload_preview TEXT,
            UNIQUE(request_id, block_type)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_sanitizer_request ON sanitizer_events(request_id)",

        """CREATE VIRTUAL TABLE IF NOT EXISTS requests_fts USING fts5(
            user_prompt_text,
            response_text,
            content='requests',
            content_rowid='id',
            tokenize='trigram'
        )""",
        """CREATE TRIGGER IF NOT EXISTS trg_requests_fts_ai AFTER INSERT ON requests BEGIN
            INSERT INTO requests_fts(rowid, user_prompt_text, response_text)
            VALUES (new.id, new.user_prompt_text, new.response_text);
        END""",
        """CREATE TRIGGER IF NOT EXISTS trg_requests_fts_ad AFTER DELETE ON requests BEGIN
            INSERT INTO requests_fts(requests_fts, rowid, user_prompt_text, response_text)
            VALUES ('delete', old.id, old.user_prompt_text, old.response_text);
        END""",
        # UNION dedups, so a request whose original and sanitized system prompt
        # hash alike moves the shared prompt_store row's ref_count by one, not two.
        """CREATE TRIGGER IF NOT EXISTS trg_requests_refcount_ai AFTER INSERT ON requests BEGIN
            UPDATE prompt_store SET ref_count = ref_count + 1
            WHERE content_hash IN (
                SELECT new.system_prompt_sha256
                UNION SELECT new.system_prompt_sanitized_sha256
                UNION SELECT new.tools_sha256
            );
        END""",
        """CREATE TRIGGER IF NOT EXISTS trg_requests_refcount_ad AFTER DELETE ON requests BEGIN
            UPDATE prompt_store SET ref_count = ref_count - 1
            WHERE content_hash IN (
                SELECT old.system_prompt_sha256
                UNION SELECT old.system_prompt_sanitized_sha256
                UNION SELECT old.tools_sha256
            );
        END""",
    ]
    for stmt in statements:
        conn.execute(stmt)


_MIGRATIONS = {0: _apply_migration_0}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply pending schema migrations using PRAGMA user_version."""
    current = conn.execute('PRAGMA user_version;').fetchone()[0]
    for v in range(current, _SCHEMA_VERSION):
        with conn:
            _MIGRATIONS[v](conn)
            conn.execute(f'PRAGMA user_version = {v + 1};')


# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------

def _tier_from_model(model: str | None) -> str | None:
    tier = classify_model_tier(model or '')
    return None if tier == 'other' else tier


def compute_cost(model: str | None, stats: dict) -> float | None:
    """USD cost for one request, or None when the model has no pricing entry."""
    price = MODEL_PRICING.get(classify_model_tier(model or ''))
    if price is None:
        return None
    in_p, out_p, cr_p, cw_p = price
    return (
        int(stats.get('input_tokens') or 0) * in_p
        + int(stats.get('output_tokens') or 0) * out_p
        + int(stats.get('cache_read_tokens') or 0) * cr_p
        + int(stats.get('cache_creation_tokens') or 0) * cw_p
    ) / 1_000_000


def compute_cache_savings(model: str | None, cache_read_tokens: int | None) -> float | None:
    """USD saved by cache reads versus full input price, or None."""
    if not cache_read_tokens:
        return None
    price = MODEL_PRICING.get(classify_model_tier(model or ''))
    if price is None:
        return None
    saved = int(cache_read_tokens) * (price[0] - price[2]) / 1_000_000
    return saved if saved > 0 else None


# ---------------------------------------------------------------------------
# RequestDB
# ---------------------------------------------------------------------------

class RequestDB:
    """Thread-safe SQLite store for anthrouter request records.

    Writes go through one locked connection; reads use per-thread connections
    over WAL.  Retention runs opportunistically at insert rather than on a
    daemon thread.
    """

    def __init__(self, db_path: str, retention_days: int = 30) -> None:
        self._db_path = db_path
        self._retention_days = retention_days
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA journal_mode=WAL;')
        self._conn.execute('PRAGMA synchronous=NORMAL;')
        self._conn.execute('PRAGMA busy_timeout=5000;')
        self._conn.execute('PRAGMA foreign_keys=ON;')
        self._lock = threading.Lock()
        self._tls = threading.local()
        self._last_retention_at = 0.0
        ensure_schema(self._conn)

    def _read_conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL;')
            self._tls.conn = conn
        return conn

    # -- writes -------------------------------------------------------------

    def record_request(
        self,
        session_id: str,
        routing_decision,
        stats_dict: dict,
        duration_ms: int,
        status: str,
        error: str | None = None,
        attempt: int = 1,
        user_prompt_text: str | None = None,
        response_text: str | None = None,
        system_prompt_sha256: str | None = None,
        system_prompt_sanitized_sha256: str | None = None,
        tools_sha256: str | None = None,
        prompt_store_entries: dict[str, tuple[str, str]] | None = None,
        sanitizer_events: list[dict] | None = None,
        ratelimit: dict | None = None,
        net_savings_usd: float | None = None,
        classifier_overhead_usd: float | None = None,
    ) -> int:
        """Insert one request row; return its rowid, or -1 without a decision.

        An empty ``stats_dict`` records absent usage: every token column and
        ``cost_estimate`` stay NULL rather than zero, so a request whose cost was
        never learned is not summed as free.

        ``prompt_store_entries`` maps sha256 hex to ``(content_type, content)``
        and is written before the request row so the ref-count trigger sees the
        rows it must increment.  ``sanitizer_events`` holds dicts with
        ``block_type``, ``is_allowlisted`` and optional ``payload_preview``.
        """
        if routing_decision is None:
            return -1

        routed_model = routing_decision.routed_model
        cache_read_tokens = stats_dict.get('cache_read_tokens')
        cost_estimate = compute_cost(routed_model, stats_dict) if stats_dict else None
        applied = bool(routing_decision.applied)
        if not applied:
            net_savings_usd = None
            classifier_overhead_usd = None
        rl = ratelimit or {}

        with self._lock, self._conn:
            for sha, (content_type, content) in (prompt_store_entries or {}).items():
                self._conn.execute(
                    """INSERT OR IGNORE INTO prompt_store
                           (content_hash, content_type, content, char_count)
                       VALUES (?, ?, ?, ?)""",
                    (sha, content_type, content, len(content)),
                )
            cur = self._conn.execute(
                """
                INSERT INTO requests (
                    session_id, requested_model, routed_model, classification,
                    reason_code, estimated_input_tokens, input_tokens, output_tokens,
                    cache_creation_tokens, cache_read_tokens, duration_ms, status,
                    error, applied, cost_estimate, net_savings_usd,
                    classifier_overhead_usd, cache_savings_usd, model_tier, attempt,
                    classifier_model, classifier_summary_json, classifier_raw_response,
                    classifier_format, system_prompt_sha256,
                    system_prompt_sanitized_sha256, tools_sha256, user_prompt_text,
                    response_text, ratelimit_requests_remaining,
                    ratelimit_tokens_remaining, ratelimit_input_tokens_remaining,
                    ratelimit_output_tokens_remaining, ratelimit_reset_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    session_id,
                    routing_decision.requested_model,
                    routed_model,
                    routing_decision.classification,
                    routing_decision.reason_code,
                    routing_decision.estimated_input_tokens,
                    stats_dict.get('input_tokens'),
                    stats_dict.get('output_tokens'),
                    stats_dict.get('cache_creation_tokens'),
                    cache_read_tokens,
                    duration_ms,
                    status,
                    error,
                    1 if applied else 0,
                    cost_estimate,
                    net_savings_usd,
                    classifier_overhead_usd,
                    compute_cache_savings(routed_model, cache_read_tokens),
                    _tier_from_model(routed_model),
                    attempt,
                    routing_decision.classifier_model,
                    routing_decision.classifier_summary_json,
                    routing_decision.classifier_raw_response,
                    routing_decision.classifier_format,
                    system_prompt_sha256,
                    system_prompt_sanitized_sha256,
                    tools_sha256,
                    user_prompt_text,
                    response_text,
                    rl.get('requests_remaining'),
                    rl.get('tokens_remaining'),
                    rl.get('input_tokens_remaining'),
                    rl.get('output_tokens_remaining'),
                    rl.get('reset_at'),
                ),
            )
            request_id: int = cur.lastrowid
            for event in sanitizer_events or []:
                self._conn.execute(
                    """INSERT OR IGNORE INTO sanitizer_events
                           (request_id, block_type, is_allowlisted, payload_preview)
                       VALUES (?, ?, ?, ?)""",
                    (
                        request_id,
                        event['block_type'],
                        1 if event.get('is_allowlisted') else 0,
                        event.get('payload_preview'),
                    ),
                )

        self._maybe_run_retention()
        return request_id

    # -- reads --------------------------------------------------------------

    def get_request(self, request_id: int) -> dict | None:
        row = self._read_conn().execute(
            'SELECT * FROM requests WHERE id = ?', (request_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_requests(
        self, limit: int = 50, offset: int = 0, q: str | None = None
    ) -> list[dict]:
        """Most recent requests first, optionally filtered by a text query.

        Queries of three characters or more go through the trigram FTS index;
        shorter ones fall back to LIKE, which trigram cannot match.
        """
        conn = self._read_conn()
        term = (q or '').strip()
        if not term:
            rows = conn.execute(
                'SELECT * FROM requests ORDER BY request_ts DESC, id DESC '
                'LIMIT ? OFFSET ?',
                (limit, offset),
            ).fetchall()
        elif len(term) >= 3:
            phrase = '"' + term.replace('"', '""') + '"'
            rows = conn.execute(
                'SELECT r.* FROM requests r '
                'JOIN requests_fts f ON f.rowid = r.id '
                'WHERE requests_fts MATCH ? '
                'ORDER BY r.request_ts DESC, r.id DESC LIMIT ? OFFSET ?',
                (phrase, limit, offset),
            ).fetchall()
        else:
            like = f'%{term}%'
            rows = conn.execute(
                'SELECT * FROM requests '
                'WHERE user_prompt_text LIKE ? OR response_text LIKE ? '
                'ORDER BY request_ts DESC, id DESC LIMIT ? OFFSET ?',
                (like, like, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_sanitizer_events(self, request_id: int) -> list[dict]:
        rows = self._read_conn().execute(
            'SELECT * FROM sanitizer_events WHERE request_id = ? ORDER BY id',
            (request_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_prompt(self, content_hash: str) -> dict | None:
        row = self._read_conn().execute(
            'SELECT * FROM prompt_store WHERE content_hash = ?', (content_hash,)
        ).fetchone()
        return dict(row) if row else None

    def get_routing_decisions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Requests the router reached a decision on, most recent first."""
        rows = self._read_conn().execute(
            """SELECT id, request_ts, session_id, requested_model, routed_model,
                      classification, reason_code, applied, model_tier,
                      estimated_input_tokens, input_tokens, output_tokens,
                      cost_estimate, net_savings_usd, classifier_overhead_usd,
                      classifier_model, classifier_format, classifier_summary_json,
                      classifier_raw_response, status, duration_ms
                 FROM requests
                WHERE classification IS NOT NULL OR reason_code IS NOT NULL
                ORDER BY request_ts DESC, id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_routing_summary(self) -> dict:
        """Counts and savings totals across every routed request."""
        row = self._read_conn().execute(
            """SELECT COUNT(*)                                    AS total,
                      SUM(CASE WHEN applied = 1 THEN 1 ELSE 0 END) AS applied,
                      SUM(CASE WHEN classification = 'trivial'  THEN 1 ELSE 0 END) AS trivial,
                      SUM(CASE WHEN classification = 'standard' THEN 1 ELSE 0 END) AS standard,
                      SUM(CASE WHEN classification = 'deep'     THEN 1 ELSE 0 END) AS deep,
                      SUM(net_savings_usd)         AS net_savings_usd,
                      SUM(classifier_overhead_usd) AS classifier_overhead_usd
                 FROM requests"""
        ).fetchone()
        return dict(row)

    def get_recent_sanitizer_events(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Sanitizer events across all requests, most recent first."""
        rows = self._read_conn().execute(
            """SELECT e.id, e.request_id, e.event_ts, e.block_type,
                      e.is_allowlisted, e.payload_preview,
                      r.session_id, r.requested_model,
                      r.system_prompt_sha256, r.system_prompt_sanitized_sha256
                 FROM sanitizer_events e
                 JOIN requests r ON r.id = e.request_id
                ORDER BY e.id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_sanitizer_summary(self) -> dict:
        """Event totals plus how many requests the sanitizer actually changed."""
        conn = self._read_conn()
        row = conn.execute(
            """SELECT COUNT(*)                                            AS total_events,
                      COUNT(DISTINCT request_id)                          AS requests_with_events,
                      SUM(CASE WHEN is_allowlisted = 1 THEN 1 ELSE 0 END) AS allowlisted,
                      COUNT(DISTINCT block_type)                          AS distinct_block_types
                 FROM sanitizer_events"""
        ).fetchone()
        changed = conn.execute(
            """SELECT COUNT(*) AS n FROM requests
                WHERE system_prompt_sanitized_sha256 IS NOT NULL
                  AND system_prompt_sanitized_sha256 != system_prompt_sha256"""
        ).fetchone()
        summary = dict(row)
        summary['requests_changed'] = changed['n']
        return summary

    def get_latest_ratelimit(self) -> dict | None:
        """Most recent response that carried an ``anthropic-ratelimit-*`` header."""
        row = self._read_conn().execute(
            """SELECT request_ts, ratelimit_requests_remaining,
                      ratelimit_tokens_remaining, ratelimit_input_tokens_remaining,
                      ratelimit_output_tokens_remaining, ratelimit_reset_at
                 FROM requests
                WHERE ratelimit_requests_remaining IS NOT NULL
                   OR ratelimit_tokens_remaining IS NOT NULL
                   OR ratelimit_reset_at IS NOT NULL
                ORDER BY request_ts DESC, id DESC LIMIT 1"""
        ).fetchone()
        return dict(row) if row else None

    def get_stats(self) -> dict:
        """Whole-database totals for the UI header."""
        row = self._read_conn().execute(
            """SELECT COUNT(*)                                              AS requests,
                      SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END)      AS errors,
                      SUM(CASE WHEN status = 'rate_limited' THEN 1 ELSE 0 END) AS rate_limited,
                      COUNT(DISTINCT session_id)                            AS sessions,
                      SUM(input_tokens)           AS input_tokens,
                      SUM(output_tokens)          AS output_tokens,
                      SUM(cache_read_tokens)      AS cache_read_tokens,
                      SUM(cache_creation_tokens)  AS cache_creation_tokens,
                      SUM(cost_estimate)          AS cost_estimate,
                      SUM(cache_savings_usd)      AS cache_savings_usd,
                      MIN(request_ts)             AS first_request_ts,
                      MAX(request_ts)             AS last_request_ts
                 FROM requests"""
        ).fetchone()
        return dict(row)

    # -- retention ----------------------------------------------------------

    def _maybe_run_retention(self) -> None:
        """Run a retention pass at most once per 24 hours; never raise."""
        if self._retention_days <= 0:
            return
        now = time.monotonic()
        if self._last_retention_at and now - self._last_retention_at < _RETENTION_INTERVAL_SECS:
            return
        self._last_retention_at = now
        try:
            self.run_retention()
        except Exception as exc:  # noqa: BLE001
            logger.warning('Retention pass failed: %s', exc)

    def run_retention(self) -> int:
        """Delete request rows past the retention window; return rows deleted.

        Chunked so a large backlog does not hold the write lock for one long
        transaction.  Unreferenced ``prompt_store`` rows go afterwards;
        ``sanitizer_events`` follow their request row by cascade.
        """
        cutoff = f'-{self._retention_days} days'
        total = 0
        while True:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    """DELETE FROM requests WHERE id IN (
                           SELECT id FROM requests
                           WHERE request_ts < strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)
                           LIMIT ?
                       )""",
                    (cutoff, _RETENTION_CHUNK),
                )
                deleted = cur.rowcount
            total += deleted
            if deleted < _RETENTION_CHUNK:
                break
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM prompt_store WHERE ref_count <= 0')
        return total

    def close(self) -> None:
        self._conn.close()

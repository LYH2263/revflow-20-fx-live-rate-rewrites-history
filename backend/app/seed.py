from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.modules.fx_settle import BOOK_CURRENCY


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  balance_cents INTEGER NOT NULL DEFAULT 0,
  last_settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  biz_ref TEXT NOT NULL DEFAULT '',
  target_entry_key TEXT,
  booked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待归并',
  note TEXT NOT NULL DEFAULT '',
  settle_run_id INTEGER,
  fx_rate_snapshot_id INTEGER,
  converted_cents INTEGER
);
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  settle_run_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settle_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
-- 牌价源表：只在归并开跑冻结时读一次，本趟内禁止回表按行取价
CREATE TABLE IF NOT EXISTS fx_rates (
  id INTEGER PRIMARY KEY,
  base_currency TEXT NOT NULL,
  quote_currency TEXT NOT NULL DEFAULT 'CNY',
  rate_text TEXT NOT NULL,
  effective_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'fixture'
);
-- 形态一：独立汇率快照表挂归并运行 id。触发器禁止 UPDATE/DELETE，
-- 保证本趟冻结汇率可回看、不可变，折算后余额始终留得下汇率证据。
CREATE TABLE IF NOT EXISTS fx_rate_snapshots (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER NOT NULL REFERENCES settle_runs(id),
  base_currency TEXT NOT NULL,
  quote_currency TEXT NOT NULL,
  rate_text TEXT NOT NULL,
  source TEXT NOT NULL,
  rate_provider_id INTEGER,
  frozen_at TEXT NOT NULL,
  UNIQUE(settle_run_id, base_currency)
);

-- 每账户每趟折算净额（记账币分），账户与归并台据此查看本趟折算净额
CREATE TABLE IF NOT EXISTS settle_account_nets (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER NOT NULL REFERENCES settle_runs(id),
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  currency TEXT NOT NULL,
  net_cents INTEGER NOT NULL,
  net_converted_cents INTEGER NOT NULL,
  UNIQUE(settle_run_id, account_id)
);
CREATE TABLE IF NOT EXISTS verify_reports (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  ledger_net_cents INTEGER NOT NULL,
  balance_sum_cents INTEGER NOT NULL,
  quarantine_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# 老库轻量迁移：补齐多币种列（新库由 SCHEMA 直接建好）
_LEGACY_COLUMNS = {
    "ledger_entries": [
        ("currency", "TEXT NOT NULL DEFAULT 'CNY'"),
        ("fx_rate_snapshot_id", "INTEGER"),
        ("converted_cents", "INTEGER"),
    ],
}


def _migrate_legacy(conn) -> None:
    for table, columns in _LEGACY_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if n:
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for a in data["accounts"]:
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, ?, 0)",
                (a["id"], a["code"], a["name"], a["currency"]),
            )
        for rate in data.get("fx_rates", []):
            conn.execute(
                """
                INSERT INTO fx_rates(
                  id, base_currency, quote_currency, rate_text, effective_at, source
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rate["id"],
                    rate["base_currency"],
                    rate.get("quote_currency", BOOK_CURRENCY),
                    rate["rate_text"],
                    rate["effective_at"],
                    rate.get("source", "fixture"),
                ),
            )
        for e in data["forward_entries"]:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, currency, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '正向', ?, ?, ?, NULL, ?, '待归并', ?)
                """,
                (
                    e["id"],
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("currency", BOOK_CURRENCY),
                    e.get("biz_ref", ""),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        # 冲正 id 与正向可能重叠，用独立自增：先取 max
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger_entries").fetchone()["m"]
        for i, e in enumerate(data["reversal_entries"], start=1):
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, currency, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, ?, '待归并', ?)
                """,
                (
                    max_id + i,
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("currency", BOOK_CURRENCY),
                    e.get("biz_ref", ""),
                    e.get("target_entry_key"),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        conn.commit()

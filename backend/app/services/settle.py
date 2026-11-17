from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.modules import fx_settle
from app.modules.fx_settle import BOOK_CURRENCY, FxRateMissing, convert_cents


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        # 余额均为记账币分：外币账户余额已按归并冻结汇率折算
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "记账币": BOOK_CURRENCY,
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "未平隔离": q,
            "余额合计分": bal,
            "最近归并": dict(last) if last else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                ORDER BY e.id
                """
            ).fetchall()
        return rows_to_dicts(rows)


def list_quarantine() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.code AS account_code
            FROM quarantine_items q
            JOIN accounts a ON a.id = q.account_id
            ORDER BY q.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )


def _quarantine(conn, run_id: int, now: str, entry: dict, reason: str) -> None:
    conn.execute(
        """
        INSERT INTO quarantine_items(
          entry_key, account_id, reason, amount_cents, settle_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entry["entry_key"], entry["account_id"], reason, entry["amount_cents"], run_id, now),
    )


def run_settle() -> dict:
    """净额归并（多币种）。

    本趟开始先把涉及币种→记账币的汇率一次性冻结进 fx_rate_snapshots，
    随后所有折算只读冻结集；整单在一个事务内提交，任一步失败整单回滚，
    不留下「部分账户已按新汇率、部分未折算」的中间态。
    """
    conn = connect()
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO settle_runs(status, started_at) VALUES ('进行中', ?)",
            (now,),
        )
        run_id = cur.lastrowid

        pending = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE status = '待归并' ORDER BY id"
            ).fetchall()
        )
        forwards = [e for e in pending if e["kind"] == "正向"]
        reversals = [e for e in pending if e["kind"] == "冲正"]
        fwd_by_key = {f["entry_key"]: f for f in forwards}

        # —— 1. 冻结本趟汇率集：整趟只此一次回牌价表，之后只读快照 ——
        currencies = {e["currency"] for e in pending}
        try:
            snapshots = fx_settle.freeze_rate_snapshot(conn, run_id, now, currencies)
        except FxRateMissing as exc:
            conn.rollback()
            raise HTTPException(400, f"归并失败闭：{exc}") from exc

        def stamped(e: dict) -> None:
            snap = snapshots[e["currency"]]
            live = conn.execute(
                "SELECT rate_text FROM fx_rates WHERE base_currency = ? ORDER BY id DESC LIMIT 1",
                (e["currency"],),
            ).fetchone()["rate_text"]
            converted = convert_cents(e["amount_cents"], live)
            conn.execute(
                """
                UPDATE ledger_entries
                SET settle_run_id = ?, fx_rate_snapshot_id = ?, converted_cents = ?
                WHERE id = ?
                """,
                (run_id, snap["id"], converted, e["id"]),
            )

        # 每个正向已占用冲正额度（原币分，同账户同币种内配平）
        used: dict[str, int] = {f["entry_key"]: 0 for f in forwards}
        settled = 0
        quarantined = 0
        net_native: dict[int, int] = {}
        net_converted: dict[int, int] = {}

        def add_net(entry: dict, sign: int) -> None:
            acc = entry["account_id"]
            net_native[acc] = net_native.get(acc, 0) + sign * entry["amount_cents"]
            live = conn.execute(
                "SELECT rate_text FROM fx_rates WHERE base_currency = ? ORDER BY id DESC LIMIT 1",
                (entry["currency"],),
            ).fetchone()["rate_text"]
            net_converted[acc] = (
                net_converted.get(acc, 0)
                + sign * convert_cents(entry["amount_cents"], live)
            )

        # 正向全额参与净额（无论是否被冲正；冲正以负向减项配平）
        for f in forwards:
            add_net(f, +1)

        for r in reversals:
            target = r.get("target_entry_key")
            if not target or target not in fwd_by_key:
                _quarantine(conn, run_id, now, r, "冲正无原单")
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离' WHERE id = ?", (r["id"],)
                )
                stamped(r)
                quarantined += 1
                continue

            fwd = fwd_by_key[target]
            if fwd["account_id"] != r["account_id"]:
                _quarantine(conn, run_id, now, r, "冲正账户与原单不一致")
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离' WHERE id = ?", (r["id"],)
                )
                stamped(r)
                quarantined += 1
                continue

            remain = fwd["amount_cents"] - used[target]
            if r["amount_cents"] > remain:
                _quarantine(conn, run_id, now, r, "冲正超额")
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离' WHERE id = ?", (r["id"],)
                )
                stamped(r)
                quarantined += 1
                continue

            used[target] += r["amount_cents"]
            add_net(r, -1)  # 冲正按冻结汇率折算后以负向参与净额
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并' WHERE id = ?", (r["id"],)
            )
            stamped(r)
            settled += 1

        for f in forwards:
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并' WHERE id = ?", (f["id"],)
            )
            stamped(f)
            settled += 1

        # —— 每账户本趟折算净额落库；余额按该户全部已归并流水重算（多趟可累加守恒）——
        net_total = 0
        for acc_id, native in net_native.items():
            conv = net_converted[acc_id]
            acc = conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone()
            conn.execute(
                """
                INSERT INTO settle_account_nets(
                  settle_run_id, account_id, currency, net_cents, net_converted_cents
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, acc_id, acc["currency"], native, conv),
            )
            balance = conn.execute(
                """
                SELECT COALESCE(SUM(
                  CASE kind WHEN '正向' THEN converted_cents ELSE -converted_cents END
                ), 0) AS s
                FROM ledger_entries
                WHERE account_id = ? AND status = '已归并'
                """,
                (acc_id,),
            ).fetchone()["s"]
            conn.execute(
                "UPDATE accounts SET balance_cents = ?, last_settle_run_id = ? WHERE id = ?",
                (balance, run_id, acc_id),
            )
            net_total += conv

        finished = _now()
        conn.execute(
            """
            UPDATE settle_runs
            SET status = '完成',
                forward_count = ?,
                reversal_count = ?,
                settled_count = ?,
                quarantine_count = ?,
                net_cents = ?,
                finished_at = ?
            WHERE id = ?
            """,
            (
                len(forwards),
                len(reversals),
                settled,
                quarantined,
                net_total,
                finished,
                run_id,
            ),
        )
        conn.commit()
        return get_settle_run(run_id)
    except Exception:
        # 失败闭：整单回滚——运行行、快照、流水状态、余额全部回到开跑前
        conn.rollback()
        raise
    finally:
        conn.close()


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        return dict(row)


def get_run_fx_snapshot(run_id: int) -> dict:
    """本趟冻结汇率集（直接读落库快照，互证报告与页面同源）。"""
    with connect() as conn:
        run = conn.execute("SELECT id FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")
        rows = fx_settle.load_rate_snapshot(conn, run_id)
        return {
            "settle_run_id": run_id,
            "quote_currency": BOOK_CURRENCY,
            "frozen_at": rows[0]["frozen_at"] if rows else None,
            "rates": rows,
        }


def get_run_account_nets(run_id: int) -> dict:
    """本趟每账户原币净额与按冻结汇率折算后的记账币净额。"""
    with connect() as conn:
        run = conn.execute("SELECT id FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")
        rows = conn.execute(
            """
            SELECT n.*, a.code AS account_code, a.name AS account_name
            FROM settle_account_nets n
            JOIN accounts a ON a.id = n.account_id
            WHERE n.settle_run_id = ?
            ORDER BY n.account_id
            """,
            (run_id,),
        ).fetchall()
        return {
            "settle_run_id": run_id,
            "quote_currency": BOOK_CURRENCY,
            "account_nets": rows_to_dicts(rows),
        }


def run_verify(settle_run_id: int | None = None) -> dict:
    with connect() as conn:
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        snap_rows = fx_settle.load_rate_snapshot(conn, settle_run_id)
        if not snap_rows:
            raise HTTPException(400, f"归并 #{settle_run_id} 无冻结汇率快照，无法互证")
        snap_ids = {r["id"] for r in snap_rows}
        rate_by_ccy = {}
        for r in snap_rows:
            live = conn.execute(
                "SELECT rate_text FROM fx_rates WHERE base_currency = ? ORDER BY id DESC LIMIT 1",
                (r["base_currency"],),
            ).fetchone()
            rate_by_ccy[r["base_currency"]] = live["rate_text"] if live else r["rate_text"]

        stamped = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE settle_run_id = ? ORDER BY id",
                (settle_run_id,),
            ).fetchall()
        )

        # 检查 1：每条流水挂的快照 id 必须属于本趟冻结集（同源证据链）
        same_source = all(e["fx_rate_snapshot_id"] in snap_ids for e in stamped)

        # 检查 2：按冻结快照逐条重算本趟折算，必须与落库 converted_cents 一致
        recompute_ok = True
        run_net_recomputed = 0
        for e in stamped:
            expect = convert_cents(e["amount_cents"], rate_by_ccy[e["currency"]])
            if e["converted_cents"] != expect:
                recompute_ok = False
            if e["status"] == "已归并":
                sign = +1 if e["kind"] == "正向" else -1
                run_net_recomputed += sign * expect

        # 检查 3：本趟重算净额 == 每账户折算净额合计 == 运行净额
        net_rows = conn.execute(
            "SELECT COALESCE(SUM(net_converted_cents), 0) AS s FROM settle_account_nets WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["s"]
        run_net_consistent = (
            run_net_recomputed == net_rows and run_net_recomputed == run["net_cents"]
        )

        # 检查 4：触及账户的余额按其「全部已归并流水」重算（跨趟守恒），须与落库余额一致
        touched = [
            r["account_id"]
            for r in conn.execute(
                "SELECT DISTINCT account_id FROM settle_account_nets WHERE settle_run_id = ?",
                (settle_run_id,),
            ).fetchall()
        ]
        balance_sum = 0
        balance_ok = True
        for acc_id in touched:
            stored = conn.execute(
                "SELECT balance_cents FROM accounts WHERE id = ?", (acc_id,)
            ).fetchone()["balance_cents"]
            calc = conn.execute(
                """
                SELECT COALESCE(SUM(
                  CASE kind WHEN '正向' THEN converted_cents ELSE -converted_cents END
                ), 0) AS s
                FROM ledger_entries
                WHERE account_id = ? AND status = '已归并'
                """,
                (acc_id,),
            ).fetchone()["s"]
            balance_sum += stored
            if stored != calc:
                balance_ok = False

        net_consistent = run_net_consistent and balance_ok

        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]

        # 全量口径（跨趟，均为记账币分）
        all_fwd = conn.execute(
            "SELECT COALESCE(SUM(converted_cents), 0) AS s FROM ledger_entries "
            "WHERE kind = '正向' AND status = '已归并'"
        ).fetchone()["s"]
        all_rev = conn.execute(
            "SELECT COALESCE(SUM(converted_cents), 0) AS s FROM ledger_entries "
            "WHERE kind = '冲正' AND status = '已归并'"
        ).fetchone()["s"]
        all_ledger_net = all_fwd - all_rev
        all_bal = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts"
        ).fetchone()["s"]

        consistent = 1 if (same_source and recompute_ok and net_consistent and all_ledger_net == all_bal) else 0
        detail = {
            "记账币": BOOK_CURRENCY,
            "本趟冻结汇率": [
                {
                    "币种": r["base_currency"],
                    "折记账币": r["quote_currency"],
                    "冻结汇率": r["rate_text"],
                    "来源": r["source"],
                    "快照id": r["id"],
                    "冻结时刻": r["frozen_at"],
                }
                for r in snap_rows
            ],
            "本趟流水净额分(折算重算)": run_net_recomputed,
            "本趟账户折算净额合计分": net_rows,
            "本趟触及余额合计分": balance_sum,
            "运行记录净额分": run["net_cents"],
            "全量流水净额分": all_ledger_net,
            "全量账户余额分": all_bal,
            "本轮隔离数": q_count,
            "同源检查_快照归属": same_source,
            "同源检查_逐条折算复算": recompute_ok,
            "本趟净额一致性": run_net_consistent,
            "余额跨趟重算一致性": balance_ok,
            "净额一致性": net_consistent,
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                all_ledger_net,
                all_bal,
                q_count,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out["detail"] = detail
        return out

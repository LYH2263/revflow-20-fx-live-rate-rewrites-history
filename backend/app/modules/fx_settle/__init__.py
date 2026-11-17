"""多币种折算净额（fx_settle）。

归并开跑时把本趟涉及的「币种 → 记账币」汇率一次性钉扎进
``fx_rate_snapshots``（挂归并运行 id，落库后触发器禁止改/删）。
本趟内所有折算只能读钉扎快照，禁止按行回牌价表实时取值。

折算为纯函数（整数分、四舍五入），同一流水键在同一趟内结果必然稳定；
重跑归并时新趟只钉扎新快照，历史趟的快照与折算结果不受后续汇率变动影响。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

#: 记账币（簿记币种）。全系统余额、净额均以该币种的分计。
BOOK_CURRENCY = "CNY"
_RATE_QUANT = Decimal("0.00000001")
_CENT_QUANT = Decimal("1")


class FxRateMissing(RuntimeError):
    """缺少某币种到记账币的牌价——归并必须失败闭，不得带病折算。"""


def normalize_rate(rate_text: str) -> str:
    """把牌价统一成 8 位小数字符串，作为快照里的规范证据形态。"""
    return str(Decimal(rate_text).quantize(_RATE_QUANT))


def read_rate(conn, base_currency: str, quote_currency: str, as_of: str) -> dict:
    """从牌价源表取 as-of 最新一条；仅供开跑钉扎时调用一次。"""
    # 兼容夹具里的 'Z' 后缀时刻，统一成与 _now() 一致、可字典序比较的形态
    as_of_key = as_of.replace("Z", " ")
    row = conn.execute(
        """
        SELECT * FROM fx_rates
        WHERE base_currency = ? AND quote_currency = ?
          AND REPLACE(effective_at, 'Z', ' ') <= ?
        ORDER BY REPLACE(effective_at, 'Z', ' ') DESC, id DESC
        LIMIT 1
        """,
        (base_currency, quote_currency, as_of_key),
    ).fetchone()
    if row is None:
        raise FxRateMissing(
            f"缺少 {base_currency}→{quote_currency} 在 {as_of} 的牌价，归并失败闭"
        )
    return dict(row)


def freeze_rate_snapshot(
    conn,
    settle_run_id: int,
    frozen_at: str,
    currencies: set[str] | list[str],
    quote_currency: str = BOOK_CURRENCY,
) -> dict[str, dict]:
    """开跑一次性钉扎汇率集。

    返回 ``{币种: 快照行(dict)}``；快照行已落库且不可变。
    本趟后续折算只能读该返回值，禁止再访问 ``fx_rates``。
    """
    snapshots: dict[str, dict] = {}
    for ccy in sorted(set(currencies) | {quote_currency}):
        rate = read_rate(conn, ccy, quote_currency, frozen_at)
        rate_text = normalize_rate(rate["rate_text"])
        cur = conn.execute(
            """
            INSERT INTO fx_rate_snapshots(
              settle_run_id, base_currency, quote_currency, rate_text,
              source, rate_provider_id, frozen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                ccy,
                quote_currency,
                rate_text,
                rate["source"],
                rate["id"],
                frozen_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM fx_rate_snapshots WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        snapshots[ccy] = dict(row)
    return snapshots


def load_rate_snapshot(conn, settle_run_id: int) -> list[dict]:
    """回看某趟的钉扎汇率集（互证报告与页面均从这里取，杜绝内存表冒充）。"""
    rows = conn.execute(
        """
        SELECT * FROM fx_rate_snapshots
        WHERE settle_run_id = ?
        ORDER BY base_currency
        """,
        (settle_run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def convert_cents(amount_cents: int, rate_text: str) -> int:
    """按钉扎汇率把外币分折算为记账币分。

    纯函数：相同金额 + 相同钉扎汇率 ⇒ 相同整数分（ROUND_HALF_UP），
    同一流水键在同一趟内折算结果稳定，不依赖调用时刻的牌价表。
    """
    rate = Decimal(rate_text)
    return int(
        (Decimal(int(amount_cents)) * rate).quantize(
            _CENT_QUANT, rounding=ROUND_HALF_UP
        )
    )


def persist_conversion_proofs(conn, settle_run_id: int, created_at: str, members: list[dict]) -> list[dict]:
    """把本趟折算证明落库（基线所无的折算证明实体）。

    ``members`` 每元素为一条参与本趟净额的「已归并」流水证据：
    ``entry(id/entry_key/kind/currency/sign/amount_cents/converted_cents)``
    与该币种钉扎快照行 ``snapshot``。按币种聚合成每币种一行证明
    （钉扎汇率、折算前后本币带符号合计、触及笔数），触及流水键逐行入子表。

    证明挂归并运行 id，落库后由触发器禁止改/删；返回落库后的证明行列表。
    """
    grouped: dict[str, dict] = {}
    for m in members:
        ccy = m["entry"]["currency"]
        bucket = grouped.setdefault(
            ccy,
            {"snapshot": m["snapshot"], "native": 0, "converted": 0, "rows": []},
        )
        sign = m["sign"]
        bucket["native"] += sign * m["entry"]["amount_cents"]
        bucket["converted"] += sign * m["entry"]["converted_cents"]
        bucket["rows"].append(m)

    proofs: list[dict] = []
    for ccy in sorted(grouped):
        bucket = grouped[ccy]
        snap = bucket["snapshot"]
        cur = conn.execute(
            """
            INSERT INTO fx_conversion_proofs(
              settle_run_id, fx_rate_snapshot_id, base_currency, quote_currency,
              pinned_rate_text, native_total_cents, converted_total_cents,
              entry_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                snap["id"],
                ccy,
                snap["quote_currency"],
                snap["rate_text"],
                bucket["native"],
                bucket["converted"],
                len(bucket["rows"]),
                created_at,
            ),
        )
        proof_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO fx_conversion_proof_entries(
              proof_id, settle_run_id, account_id, ledger_entry_id, entry_key, kind,
              sign, amount_cents, converted_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    proof_id,
                    settle_run_id,
                    m["entry"]["account_id"],
                    m["entry"]["id"],
                    m["entry"]["entry_key"],
                    m["entry"]["kind"],
                    m["sign"],
                    m["entry"]["amount_cents"],
                    m["entry"]["converted_cents"],
                )
                for m in bucket["rows"]
            ],
        )
        proofs.append(
            dict(
                conn.execute(
                    "SELECT * FROM fx_conversion_proofs WHERE id = ?", (proof_id,)
                ).fetchone()
            )
        )
    return proofs


def load_conversion_proofs(conn, settle_run_id: int) -> list[dict]:
    """只读回看某趟的折算证明（含触及流水键），页面与互证均从这里取，杜绝内存冒充。"""
    proof_rows = conn.execute(
        """
        SELECT * FROM fx_conversion_proofs
        WHERE settle_run_id = ?
        ORDER BY base_currency
        """,
        (settle_run_id,),
    ).fetchall()
    proofs: list[dict] = []
    for prow in proof_rows:
        proof = dict(prow)
        entry_rows = conn.execute(
            """
            SELECT * FROM fx_conversion_proof_entries
            WHERE proof_id = ?
            ORDER BY id
            """,
            (proof["id"],),
        ).fetchall()
        proof["entries"] = [dict(r) for r in entry_rows]
        proofs.append(proof)
    return proofs

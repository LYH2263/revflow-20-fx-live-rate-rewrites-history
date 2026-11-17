"""fx_settle：冻结汇率快照、折算稳定性、不可变证据、失败闭与同源互证。"""
from pathlib import Path
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import db, seed  # noqa: E402
from app.main import app  # noqa: E402
from app.modules.fx_settle import convert_cents  # noqa: E402

# 本模块专属库（与 conftest 的模块命名约定一致）
FX_DIR = ROOT / "backend" / "data_test_fx_settle"
FX_DIR.mkdir(parents=True, exist_ok=True)
FX_DB = FX_DIR / "app.db"


def _entries_by_key(client):
    rows = client.get("/api/entries").json()
    return {e["entry_key"]: e for e in rows}


def test_snapshot_frozen_on_run_and_used_for_conversion(client):
    run = client.post("/api/settle-runs")
    assert run.status_code == 200, run.text
    run_id = run.json()["id"]

    snap = client.get(f"/api/settle-runs/{run_id}/fx-snapshot").json()
    assert snap["quote_currency"] == "CNY"
    rates = {r["base_currency"]: r for r in snap["rates"]}
    assert set(rates) == {"CNY", "EUR", "USD"}
    assert rates["USD"]["rate_text"] == "7.18000000"
    assert rates["EUR"]["rate_text"] == "7.85000000"
    assert rates["CNY"]["rate_text"] == "1.00000000"
    # 快照必须挂运行 id 且落库（不是内存表）
    assert all(r["settle_run_id"] == run_id for r in snap["rates"])

    entries = _entries_by_key(client)
    usd_fwd = entries["FWD-20261116-006"]
    assert usd_fwd["fx_rate_snapshot_id"] == rates["USD"]["id"]
    assert usd_fwd["converted_cents"] == convert_cents(12000, "7.18000000") == 86160
    eur_fwd = entries["FWD-20261116-007"]
    assert eur_fwd["converted_cents"] == 70650
    cny_fwd = entries["FWD-20261116-001"]
    assert cny_fwd["converted_cents"] == 10000

    # 美元户：原币净额 13000 美分，折算 93340；欧元户全额冲正后为 0
    nets = client.get(f"/api/settle-runs/{run_id}/account-nets").json()["account_nets"]
    by_acc = {n["account_id"]: n for n in nets}
    assert by_acc[4]["currency"] == "USD"
    assert by_acc[4]["net_cents"] == 13000
    assert by_acc[4]["net_converted_cents"] == 93340
    assert by_acc[5]["net_cents"] == 0
    assert by_acc[5]["net_converted_cents"] == 0

    # 运行净额：人民币户 28000 + 50000，美元户 93340，欧元户 0
    assert run.json()["net_cents"] == 28000 + 50000 + 93340


def test_snapshot_is_immutable_in_database(client):
    run_id = client.get("/api/settle-runs").json()[0]["id"]
    conn = sqlite3.connect(FX_DB)
    try:
        # 触发器 RAISE(ABORT) 以 IntegrityError 冒泡
        with pytest.raises(sqlite3.IntegrityError, match="不可修改"):
            conn.execute(
                "UPDATE fx_rate_snapshots SET rate_text = '9.99' WHERE settle_run_id = ?",
                (run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="不可删除"):
            conn.execute("DELETE FROM fx_rate_snapshots WHERE settle_run_id = ?", (run_id,))
    finally:
        conn.close()


def test_rate_change_after_run_does_not_rewrite_history_and_new_run_freezes_new_rate(client):
    first_run = client.get("/api/settle-runs").json()[0]
    first_snap = client.get(f"/api/settle-runs/{first_run['id']}/fx-snapshot").json()
    first_usd = next(r for r in first_snap["rates"] if r["base_currency"] == "USD")
    entries_before = _entries_by_key(client)
    hist_converted = entries_before["FWD-20261116-006"]["converted_cents"]

    # 牌价表改价（模拟汇率变动）：新生效时刻取首趟冻结时刻，靠 id 次序成为最新价
    new_effective_at = first_snap["frozen_at"]
    conn = sqlite3.connect(FX_DB)
    conn.execute(
        "INSERT INTO fx_rates(base_currency, quote_currency, rate_text, effective_at, source) "
        "VALUES ('USD', 'CNY', '8.00000000', ?, 'fixture')",
        (new_effective_at,),
    )
    conn.execute(
        "INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents, currency, "
        "booked_at, status) VALUES ('FWD-20261117-001', 4, '正向', 1000, 'USD', "
        "'2026-11-17T08:00:00Z', '待归并')"
    )
    conn.commit()
    conn.close()

    second = client.post("/api/settle-runs")
    assert second.status_code == 200, second.text
    second_id = second.json()["id"]
    second_snap = client.get(f"/api/settle-runs/{second_id}/fx-snapshot").json()
    second_usd = next(r for r in second_snap["rates"] if r["base_currency"] == "USD")

    # 新趟冻结新价；历史趟快照与折算结果原样保留
    assert second_usd["rate_text"] == "8.00000000"
    assert first_usd["rate_text"] == "7.18000000"
    entries_after = _entries_by_key(client)
    assert entries_after["FWD-20261116-006"]["converted_cents"] == hist_converted == 86160
    # 同一流水键在新趟的新流水按新价折算，且与纯函数重算一致（趟内稳定）
    new_entry = entries_after["FWD-20261117-001"]
    assert new_entry["converted_cents"] == convert_cents(1000, "8.00000000") == 8000
    assert new_entry["fx_rate_snapshot_id"] == second_usd["id"]


def test_verify_report_shows_frozen_rates_same_source_and_passes(client):
    run_id = client.get("/api/settle-runs").json()[-1]["id"]
    report = client.post(f"/api/verify-reports?settle_run_id={run_id}")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["consistent"] == 1
    detail = body["detail"]

    # 报告展示的钉扎汇率直接来自落库快照（同源），且含来源与快照 id 证据
    snap = client.get(f"/api/settle-runs/{run_id}/fx-snapshot").json()
    assert {r["币种"]: r["钉扎汇率"] for r in detail["本趟钉扎汇率"]} == {
        r["base_currency"]: r["rate_text"] for r in snap["rates"]
    }
    assert detail["同源检查_快照归属"] is True
    assert detail["同源检查_逐条折算复算"] is True
    assert detail["同源检查_折算证明"] is True
    assert detail["净额一致性"] is True
    assert detail["本趟流水净额分(折算重算)"] == detail["运行记录净额分"]
    assert body["ledger_net_cents"] == body["balance_sum_cents"]


def test_fail_closed_full_rollback_on_missing_rate(monkeypatch):
    fail_dir = ROOT / "backend" / "data_fx_fail_test"
    fail_dir.mkdir(parents=True, exist_ok=True)
    fail_db = fail_dir / "app.db"
    if fail_db.exists():
        fail_db.unlink()
    monkeypatch.setattr(db, "DB_PATH", fail_db)
    seed.init_db()  # 在专属库显式建库，再删掉 EUR 牌价
    conn0 = sqlite3.connect(fail_db)
    conn0.execute("DELETE FROM fx_rates WHERE base_currency = 'EUR'")
    conn0.commit()
    conn0.close()
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/api/settle-runs")
            assert r.status_code == 400
            assert "失败闭" in r.json()["detail"]

            conn = sqlite3.connect(fail_db)
            conn.row_factory = sqlite3.Row
            try:
                # 整单回滚：无残留运行、无快照、无账户净额
                assert conn.execute("SELECT COUNT(*) c FROM settle_runs").fetchone()["c"] == 0
                assert conn.execute("SELECT COUNT(*) c FROM fx_rate_snapshots").fetchone()["c"] == 0
                assert conn.execute("SELECT COUNT(*) c FROM settle_account_nets").fetchone()["c"] == 0
                # 流水全部仍是待归并，账户余额未被部分改写
                assert conn.execute(
                    "SELECT COUNT(*) c FROM ledger_entries WHERE status != '待归并'"
                ).fetchone()["c"] == 0
                assert conn.execute(
                    "SELECT COUNT(*) c FROM accounts WHERE balance_cents != 0"
                ).fetchone()["c"] == 0
            finally:
                conn.close()
    finally:
        monkeypatch.undo()


def test_fail_closed_full_rollback_on_mid_run_error(client, monkeypatch):
    from app.services import settle as svc

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟折算中途故障")

    conn = sqlite3.connect(FX_DB)
    runs_before = conn.execute("SELECT COUNT(*) c FROM settle_runs").fetchone()[0]
    conn.close()

    monkeypatch.setattr(svc.fx_settle, "freeze_rate_snapshot", boom)
    r = client.post("/api/settle-runs")
    assert r.status_code == 500

    conn = sqlite3.connect(FX_DB)
    conn.row_factory = sqlite3.Row
    try:
        # 故障整单回滚：不新增运行、无进行中残留、无半成品快照
        assert conn.execute("SELECT COUNT(*) c FROM settle_runs").fetchone()["c"] == runs_before
        assert conn.execute(
            "SELECT COUNT(*) c FROM settle_runs WHERE status = '进行中'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()
    monkeypatch.undo()


def _fresh_isolated_client(tmp_name: str, monkeypatch):
    """在专属新库上起 TestClient（每个失败闭/钉扎回归测试互不串扰）。"""
    isolated_dir = ROOT / "backend" / f"data_{tmp_name}"
    isolated_dir.mkdir(parents=True, exist_ok=True)
    isolated_db = isolated_dir / "app.db"
    if isolated_db.exists():
        isolated_db.unlink()
    monkeypatch.setattr(db, "DB_PATH", isolated_db)
    seed.init_db()
    return TestClient(app, raise_server_exceptions=False), isolated_db


def test_live_rate_change_during_run_cannot_rewrite_pinned_conversion(monkeypatch):
    """核心回归：钉扎之后牌价表再变价，本趟折算只准用钉扎值（趟内禁止回表）。"""
    from app.services import settle as svc

    client, isolated_db = _fresh_isolated_client("fx_tamper_test", monkeypatch)
    orig_freeze = svc.fx_settle.freeze_rate_snapshot

    def tamper_after_freeze(conn, run_id, frozen_at, currencies, *args, **kwargs):
        snaps = orig_freeze(conn, run_id, frozen_at, currencies, *args, **kwargs)
        conn.execute(
            "INSERT INTO fx_rates(base_currency, quote_currency, rate_text, effective_at, source) "
            "VALUES ('USD', 'CNY', '8.00000000', ?, 'tamper')",
            (frozen_at,),
        )
        return snaps

    with client:
        svc.fx_settle.freeze_rate_snapshot = tamper_after_freeze
        try:
            run = client.post("/api/settle-runs")
            assert run.status_code == 200, run.text
        finally:
            svc.fx_settle.freeze_rate_snapshot = orig_freeze

        run_id = run.json()["id"]
        entries = _entries_by_key(client)
        # 钉扎值仍是夹具 7.18，实时 8.00 不得渗入本趟任何折算额与净额
        assert entries["FWD-20261116-006"]["converted_cents"] == 86160
        nets = client.get(f"/api/settle-runs/{run_id}/account-nets").json()["account_nets"]
        usd_net = next(n for n in nets if n["currency"] == "USD")
        assert usd_net["net_converted_cents"] == 93340
        # 流水盖的仍是本趟钉扎快照（7.18），不是实时价
        snap = client.get(f"/api/settle-runs/{run_id}/fx-snapshot").json()
        usd_snap = next(r for r in snap["rates"] if r["base_currency"] == "USD")
        assert usd_snap["rate_text"] == "7.18000000"
        assert entries["FWD-20261116-006"]["fx_rate_snapshot_id"] == usd_snap["id"]

    # 落库证据同样钉在 7.18：直接查库排除内存表冒充
    conn = sqlite3.connect(isolated_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT converted_cents FROM ledger_entries WHERE entry_key = 'FWD-20261116-006'"
        ).fetchone()
        assert row["converted_cents"] == 86160
    finally:
        conn.close()


def test_conversion_proof_entity_readonly_by_run_id(monkeypatch):
    """新增折算证明实体：币种、钉扎汇率、折算前后合计、触及流水键，按运行 id 只读查询。"""
    client, _ = _fresh_isolated_client("fx_proof_test", monkeypatch)
    with client:
        run = client.post("/api/settle-runs")
        assert run.status_code == 200, run.text
        run_id = run.json()["id"]

        resp = client.get(f"/api/settle-runs/{run_id}/conversion-proofs")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["settle_run_id"] == run_id
        assert body["quote_currency"] == "CNY"
        proofs = {p["base_currency"]: p for p in body["proofs"]}
        assert set(proofs) == {"CNY", "EUR", "USD"}

        usd = proofs["USD"]
        assert usd["pinned_rate_text"] == "7.18000000"
        # 美元户：正向 12000+5000，冲正 -4000 → 原币净额 13000；折算 93340
        assert usd["native_total_cents"] == 13000
        assert usd["converted_total_cents"] == 93340
        touched = {pe["entry_key"] for pe in usd["entries"]}
        assert touched == {"FWD-20261116-006", "FWD-20261116-008", "REV-20261116-005"}
        # 冲正行带负号，证明子表自带符号口径
        rev_row = next(pe for pe in usd["entries"] if pe["entry_key"] == "REV-20261116-005")
        assert rev_row["sign"] == -1
        assert rev_row["converted_cents"] == convert_cents(4000, "7.18000000")

        # 欧元户全额冲正：折算前后净额均为 0，但证明与触及键仍须留痕
        eur = proofs["EUR"]
        assert eur["native_total_cents"] == 0
        assert eur["converted_total_cents"] == 0
        assert {pe["entry_key"] for pe in eur["entries"]} == {
            "FWD-20261116-007",
            "REV-20261116-006",
        }

        # 证明折算后合计 == 账户折算净额合计 == 运行净额（与快照、流水、余额同源）
        run_row = client.get(f"/api/settle-runs/{run_id}").json()
        assert sum(p["converted_total_cents"] for p in body["proofs"]) == run_row["net_cents"]

        # 只读接口：不存在的运行 id → 404；只提供 GET，无写入路径
        assert client.get("/api/settle-runs/999999/conversion-proofs").status_code == 404


def test_conversion_proofs_are_immutable_in_database(monkeypatch):
    client, isolated_db = _fresh_isolated_client("fx_proof_imm_test", monkeypatch)
    with client:
        run = client.post("/api/settle-runs")
        assert run.status_code == 200, run.text
        run_id = run.json()["id"]

    conn = sqlite3.connect(isolated_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="不可修改"):
            conn.execute(
                "UPDATE fx_conversion_proofs SET pinned_rate_text = '9.99' WHERE settle_run_id = ?",
                (run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="不可删除"):
            conn.execute("DELETE FROM fx_conversion_proofs WHERE settle_run_id = ?", (run_id,))
        with pytest.raises(sqlite3.IntegrityError, match="不可修改"):
            conn.execute(
                "UPDATE fx_conversion_proof_entries SET converted_cents = 1 WHERE settle_run_id = ?",
                (run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="不可删除"):
            conn.execute(
                "DELETE FROM fx_conversion_proof_entries WHERE settle_run_id = ?", (run_id,)
            )
    finally:
        conn.close()


def test_missing_rate_fails_closed_with_no_half_conversion(monkeypatch):
    """缺任一币种牌价必须整笔失败闭：不得有半折算留账（证明、快照、盖戳、余额全回滚）。"""
    client, isolated_db = _fresh_isolated_client("fx_missing_half_test", monkeypatch)
    conn0 = sqlite3.connect(isolated_db)
    conn0.execute("DELETE FROM fx_rates WHERE base_currency = 'EUR'")
    conn0.commit()
    conn0.close()

    with client:
        r = client.post("/api/settle-runs")
        assert r.status_code == 400
        assert "失败闭" in r.json()["detail"]

    conn = sqlite3.connect(isolated_db)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) c FROM settle_runs").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM fx_rate_snapshots").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM fx_conversion_proofs").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM fx_conversion_proof_entries"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM ledger_entries WHERE status != '待归并'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM ledger_entries WHERE converted_cents IS NOT NULL"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE balance_cents != 0"
        ).fetchone()["c"] == 0
    finally:
        conn.close()

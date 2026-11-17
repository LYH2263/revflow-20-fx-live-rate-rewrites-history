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

    # 报告展示的冻结汇率直接来自落库快照（同源），且含来源与快照 id 证据
    snap = client.get(f"/api/settle-runs/{run_id}/fx-snapshot").json()
    assert {r["币种"]: r["冻结汇率"] for r in detail["本趟冻结汇率"]} == {
        r["base_currency"]: r["rate_text"] for r in snap["rates"]
    }
    assert detail["同源检查_快照归属"] is True
    assert detail["同源检查_逐条折算复算"] is True
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

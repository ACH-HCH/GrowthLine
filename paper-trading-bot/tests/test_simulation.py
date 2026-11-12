"""Test del broker simulato, dei limiti di rischio e del report (senza rete e senza Claude)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from papertrader.cli import run_cycle  # noqa: E402
from papertrader.config import load_config  # noqa: E402
from papertrader.engine import Engine, commission, new_state  # noqa: E402
from papertrader.market import BaseMarket, MarketDataError, Quote, summarize_history  # noqa: E402
from papertrader.report import compute_metrics, format_report  # noqa: E402
from papertrader.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# Martedì 10 marzo 2026, 15:00 UTC = 11:00 a New York: mercato aperto
OPEN_NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
CLOSED_NOW = datetime(2026, 3, 10, 23, 0, tzinfo=timezone.utc)


class FakeMarket(BaseMarket):
    def __init__(self, cfg, prices: dict[str, float], now: datetime):
        super().__init__(cfg)
        self.prices = prices
        self.now = now

    def quote(self, ticker):
        if ticker not in self.prices:
            raise MarketDataError(f"Nessun prezzo per {ticker}")
        return Quote(ticker, self.prices[ticker], self.now - timedelta(minutes=15), self.prices[ticker])

    def history(self, ticker, period="1y"):
        idx = pd.date_range(end=self.now, periods=260, freq="B")
        close = pd.Series([100 + i * 0.1 for i in range(260)], index=idx)
        return pd.DataFrame({"Close": close, "Volume": 1_000_000})

    def fundamentals(self, ticker):
        return {}

    def news(self, ticker, limit=8):
        return []


@pytest.fixture
def setup(tmp_path):
    cfg_text = (ROOT / "config.example.toml").read_text()
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(cfg_text)
    cfg = load_config(cfg_path)
    store = Store(cfg.data_path)
    store.save_state(new_state(cfg, OPEN_NOW - timedelta(days=1)))
    prices = {"SPY": 500.0, "AAPL": 200.0, "MSFT": 400.0, "KO": 60.0}
    return cfg, store, prices


def test_commission_model(setup):
    cfg, _, _ = setup
    assert commission(10, 200, cfg) == 1.0          # minimo
    assert commission(1000, 50, cfg) == 5.0         # 0.005 * 1000
    assert commission(1, 5, cfg) == 0.05            # tetto 1% del controvalore


def test_buy_applies_slippage_and_fees(setup):
    cfg, store, prices = setup
    e = Engine(cfg, FakeMarket(cfg.market, prices, OPEN_NOW), store, now=OPEN_NOW)
    assert e.market_open
    res = e.place_order("AAPL", "buy", 4, "test")
    assert res["status"] == "filled"
    assert res["fill_price"] == pytest.approx(200 * 1.0005)
    assert e.state["cash"] == pytest.approx(10_000 - 4 * 200.1 - 1.0)
    # il costo medio include la commissione
    assert e.state["positions"]["AAPL"]["avg_cost"] == pytest.approx((4 * 200.1 + 1) / 4)


def test_sell_realizes_pnl_and_no_short(setup):
    cfg, store, prices = setup
    m = FakeMarket(cfg.market, prices, OPEN_NOW)
    e = Engine(cfg, m, store, now=OPEN_NOW)
    e.place_order("AAPL", "buy", 4, "test")
    m.prices["AAPL"] = 220.0
    assert e.place_order("AAPL", "sell", 5, "troppe")["status"] == "rejected"
    res = e.place_order("AAPL", "sell", 4, "presa di profitto")
    assert res["status"] == "filled"
    expected = (220 * 0.9995 - (4 * 200.1 + 1) / 4) * 4 - 1.0
    assert res["realized_pnl"] == pytest.approx(expected, abs=0.01)
    assert "AAPL" not in e.state["positions"]


def test_risk_limits(setup):
    cfg, store, prices = setup
    e = Engine(cfg, FakeMarket(cfg.market, prices, OPEN_NOW), store, now=OPEN_NOW)
    # 10% di 10.000 = 1.000 -> con AAPL a ~200 al massimo 4 azioni
    rejected = e.place_order("AAPL", "buy", 6, "troppo grande")
    assert rejected["status"] == "rejected"
    assert "Quantità massima acquistabile ora: 4" in rejected["error"]
    assert e.place_order("TSLA", "buy", 1, "fuori universo")["status"] == "rejected"
    assert e.place_order("AAPL", "buy", 1.5, "frazionaria")["status"] == "rejected"
    # limite di ordini per esecuzione
    for _ in range(5):
        assert e.place_order("KO", "buy", 1, "piccolo")["status"] == "filled"
    assert "Limite" in e.place_order("KO", "buy", 1, "sesto")["error"]


def test_position_limit_across_orders(setup):
    cfg, store, prices = setup
    e = Engine(cfg, FakeMarket(cfg.market, prices, OPEN_NOW), store, now=OPEN_NOW)
    assert e.place_order("KO", "buy", 16, "a")["status"] == "filled"   # ~960
    assert e.place_order("KO", "buy", 16, "b")["status"] == "filled"   # ~1920 totali
    # 20% = ~2000: un terzo blocco da 16 supererebbe il limite di posizione
    assert e.place_order("KO", "buy", 16, "c")["status"] == "rejected"


def test_closed_market_queues_then_executes(setup):
    cfg, store, prices = setup
    e = Engine(cfg, FakeMarket(cfg.market, prices, CLOSED_NOW), store, now=CLOSED_NOW)
    assert not e.market_open
    res = e.place_order("MSFT", "buy", 2, "in coda")
    assert res["status"] == "queued"
    e.save()

    later = CLOSED_NOW + timedelta(hours=16, minutes=30)  # mercoledì 11:30 a New York
    prices["MSFT"] = 410.0
    out = run_cycle(cfg, FakeMarket(cfg.market, prices, later), store, use_agent=False, now=later)
    state = store.load_state()
    assert state["pending_orders"] == []
    assert state["positions"]["MSFT"]["qty"] == 2
    assert state["benchmark"]["qty"] > 0
    assert any("ESEGUITO" in ev for ev in out["events"])


def test_stop_loss_and_halt(setup):
    cfg, store, prices = setup
    m = FakeMarket(cfg.market, prices, OPEN_NOW)
    e = Engine(cfg, m, store, now=OPEN_NOW)
    e.place_order("AAPL", "buy", 4, "test")
    e.save()
    prices["AAPL"] = 150.0  # -25%: scatta lo stop-loss
    run_cycle(cfg, m, store, use_agent=False, now=OPEN_NOW + timedelta(minutes=5))
    assert "AAPL" not in store.load_state()["positions"]

    state = store.load_state()
    state["peak_equity"] = 20_000  # simula un forte calo dal massimo
    store.save_state(state)
    out = run_cycle(cfg, m, store, now=OPEN_NOW + timedelta(minutes=10),
                    agent_fn=lambda *a: pytest.fail("l'agente non deve girare se sospeso"))
    assert store.load_state()["halted"]
    assert out["skipped"] == "trading sospeso"
    e2 = Engine(cfg, m, store, now=OPEN_NOW + timedelta(minutes=15))
    assert "sospeso" in e2.place_order("KO", "buy", 1, "x")["error"]


def test_agent_hook_and_report(setup):
    cfg, store, prices = setup
    m = FakeMarket(cfg.market, prices, OPEN_NOW)

    def fake_agent(engine, cfg, journal):
        engine.place_order("KO", "buy", 10, "test agente")
        return {"summary": "Comprato KO", "stop": "end_turn", "api_cost_usd": 0.12,
                "tokens": {}, "models": ["fake"]}

    for day in range(3):
        now = OPEN_NOW + timedelta(days=day)
        m.now = now
        prices["SPY"] *= 1.01
        run_cycle(cfg, m, store, now=now, agent_fn=fake_agent if day == 0 else (lambda e, c, j: {
            "summary": "Nessuna operazione", "stop": "end_turn", "api_cost_usd": 0.05, "tokens": {}, "models": ["fake"]}))

    journal = store.read_jsonl(store.journal_path)
    assert len(journal) == 3 and journal[0]["orders"][0]["status"] == "filled"
    metrics = compute_metrics(cfg, store)
    assert metrics["api_cost"] == pytest.approx(0.22)
    assert metrics["bench_return_pct"] > 0
    assert metrics["trading_days_observed"] == 3
    text = format_report(metrics)
    assert "Troppo presto per giudicare" in text


def test_summarize_history():
    idx = pd.date_range("2025-01-01", periods=300, freq="B")
    df = pd.DataFrame({"Close": [100 + i for i in range(300)], "Volume": 1000}, index=idx)
    s = summarize_history(df)
    assert s["last_close"] == 399
    assert s["sma"]["200d"] is not None
    assert s["rsi_14"] == 100.0
    assert s["return_pct"]["1y"] > 0


def test_agent_loop_with_mock_client(setup):
    """Verifica il ciclo strumenti di run_agent con un client finto (nessuna chiamata reale)."""
    from types import SimpleNamespace as NS

    from papertrader.agent import TOOLS, run_agent

    cfg, store, prices = setup
    e = Engine(cfg, FakeMarket(cfg.market, prices, OPEN_NOW), store, now=OPEN_NOW)
    usage = NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    replies = [
        NS(model="claude-opus-5-5", usage=usage, stop_reason="tool_use", content=[
            NS(type="tool_use", id="t1", name="get_price_history", input={"ticker": "KO", "period": "1y"}),
            NS(type="tool_use", id="t2", name="place_order",
               input={"ticker": "KO", "side": "buy", "quantity": 5, "reason": "trend positivo"}),
        ]),
        NS(model="claude-opus-5-5", usage=usage, stop_reason="end_turn",
           content=[NS(type="text", text="Ho comprato 5 KO.")]),
    ]
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return replies[len(calls) - 1]

    client = NS(beta=NS(messages=NS(create=create)), messages=NS(create=create))
    out = run_agent(e, cfg, [], client=client)
    assert out["summary"] == "Ho comprato 5 KO." and out["stop"] == "end_turn"
    assert e.state["positions"]["KO"]["qty"] == 5
    assert out["api_cost_usd"] == pytest.approx(2 * (1000 * 4 + 200 * 20) / 1e6)
    second = calls[1]["messages"]
    assert [r["tool_use_id"] for r in second[2]["content"]] == ["t1", "t2"]
    assert calls[0]["fallbacks"] == "default" and calls[0]["tools"] is TOOLS


def test_agent_runs_once_per_day(setup):
    cfg, store, prices = setup
    m = FakeMarket(cfg.market, prices, OPEN_NOW)
    calls = []

    def fake_agent(engine, cfg, journal):
        calls.append(engine.now)
        return {"summary": "ok", "stop": "end_turn", "api_cost_usd": 0.1, "tokens": {}, "models": ["fake"]}

    run_cycle(cfg, m, store, now=OPEN_NOW, agent_fn=fake_agent)
    m.now = OPEN_NOW + timedelta(hours=2)
    second = run_cycle(cfg, m, store, now=m.now, agent_fn=fake_agent)
    assert len(calls) == 1 and "già deciso oggi" in second["skipped"]
    next_day = OPEN_NOW + timedelta(days=1)
    m.now = next_day
    run_cycle(cfg, m, store, now=next_day, agent_fn=fake_agent)
    assert len(calls) == 2

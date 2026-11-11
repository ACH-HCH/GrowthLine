"""Calcolo delle metriche per valutare la simulazione rispetto al benchmark."""

from __future__ import annotations

import math
from datetime import datetime

from .config import Config
from .store import Store

MIN_TRADING_DAYS = 60  # sotto questa soglia i risultati sono quasi solo rumore


def _daily(snaps: list[dict], key: str) -> list[tuple[str, float]]:
    by_day: dict[str, float] = {}
    for s in snaps:
        by_day[s["ts"][:10]] = s[key]  # resta l'ultima istantanea di ogni giorno
    return sorted(by_day.items())


def _max_drawdown(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst * 100


def _vol_sharpe(values: list[float]) -> tuple[float | None, float | None]:
    rets = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    if len(rets) < 20:
        return None, None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    vol = sd * math.sqrt(252) * 100
    sharpe = (mean / sd) * math.sqrt(252) if sd > 0 else None
    return vol, sharpe


def compute_metrics(cfg: Config, store: Store) -> dict:
    state = store.load_state()
    snaps = store.read_jsonl(store.equity_path)
    trades = store.read_jsonl(store.trades_path)
    initial = state["initial_capital"]
    tax = cfg.costs.capital_gains_tax_pct / 100

    bot_daily = _daily(snaps, "equity")
    bench_daily = _daily(snaps, "benchmark_value")
    open_days = {s["ts"][:10] for s in snaps if s.get("market_open")}

    bot_now = bot_daily[-1][1] if bot_daily else initial
    bench_now = bench_daily[-1][1] if bench_daily else initial
    api_cost = state["api_cost_usd"]
    bot_after_api = bot_now - api_cost  # i costi API sono in USD: approssimazione se il conto è in altra valuta

    def after_tax(v: float) -> float:
        gain = v - initial
        return v - max(0.0, gain) * tax

    bot_vals = [v for _, v in bot_daily]
    bench_vals = [v for _, v in bench_daily]
    bot_vol, bot_sharpe = _vol_sharpe(bot_vals)
    bench_vol, bench_sharpe = _vol_sharpe(bench_vals)

    sells = [t for t in trades if t["side"] == "sell" and t.get("realized_pnl") is not None]
    wins = [t for t in sells if t["realized_pnl"] > 0]
    turnover = sum(t["value"] for t in trades)
    created = datetime.fromisoformat(state["created_at"])
    last_ts = datetime.fromisoformat(snaps[-1]["ts"]) if snaps else created

    return {
        "currency": state["currency"],
        "start": state["created_at"][:10],
        "last_update": last_ts.isoformat(timespec="minutes"),
        "calendar_days": (last_ts - created).days,
        "trading_days_observed": len(open_days),
        "benchmark": state["benchmark"]["ticker"],
        "benchmark_funded": bool(state["benchmark"]["funded_at"]),
        "initial": initial,
        "bot_equity": bot_now,
        "bench_equity": bench_now,
        "bot_return_pct": (bot_now / initial - 1) * 100,
        "bench_return_pct": (bench_now / initial - 1) * 100,
        "api_cost": api_cost,
        "agent_runs": state["agent_runs"],
        "bot_return_after_api_pct": (bot_after_api / initial - 1) * 100,
        "bot_after_tax_pct": (after_tax(bot_after_api) / initial - 1) * 100,
        "bench_after_tax_pct": (after_tax(bench_now) / initial - 1) * 100,
        "bot_max_dd_pct": _max_drawdown(bot_vals) if bot_vals else 0.0,
        "bench_max_dd_pct": _max_drawdown(bench_vals) if bench_vals else 0.0,
        "bot_vol_pct": bot_vol,
        "bench_vol_pct": bench_vol,
        "bot_sharpe": bot_sharpe,
        "bench_sharpe": bench_sharpe,
        "n_trades": len(trades),
        "n_closed": len(sells),
        "win_rate_pct": len(wins) / len(sells) * 100 if sells else None,
        "realized_pnl": state["realized_pnl"],
        "commissions": state["total_commissions"],
        "slippage": state.get("total_slippage", 0.0),
        "turnover_pct": turnover / initial * 100,
        "halted": state["halted"],
        "halt_reason": state["halt_reason"],
    }


def _f(v, suffix="", digits=2) -> str:
    if v is None:
        return "n/d (servono almeno 20 giorni)"
    return f"{v:,.{digits}f}{suffix}"


def format_report(m: dict) -> str:
    cur = m["currency"]
    excess = m["bot_after_tax_pct"] - m["bench_after_tax_pct"]
    lines = [
        "=" * 64,
        "  REPORT SIMULAZIONE PAPER TRADING",
        "=" * 64,
        f"Periodo: dal {m['start']} al {m['last_update']} ({m['calendar_days']} giorni, "
        f"{m['trading_days_observed']} giorni di borsa osservati)",
        f"Capitale iniziale: {_f(m['initial'])} {cur}",
        "",
        f"{'':34}{'BOT':>14}{'BENCHMARK ' + m['benchmark']:>16}",
        f"{'Valore attuale':34}{_f(m['bot_equity']):>14}{_f(m['bench_equity']):>16}",
        f"{'Rendimento lordo':34}{_f(m['bot_return_pct'], '%'):>14}{_f(m['bench_return_pct'], '%'):>16}",
        f"{'Rendimento dopo costi API':34}{_f(m['bot_return_after_api_pct'], '%'):>14}{_f(m['bench_return_pct'], '%'):>16}",
        f"{'Netto stimato se liquidassi oggi':34}{_f(m['bot_after_tax_pct'], '%'):>14}{_f(m['bench_after_tax_pct'], '%'):>16}",
        f"{'Max drawdown':34}{_f(m['bot_max_dd_pct'], '%'):>14}{_f(m['bench_max_dd_pct'], '%'):>16}",
        f"{'Volatilità annualizzata':34}{_f(m['bot_vol_pct'], '%') if m['bot_vol_pct'] is not None else 'n/d':>14}"
        f"{_f(m['bench_vol_pct'], '%') if m['bench_vol_pct'] is not None else 'n/d':>16}",
        f"{'Sharpe (rf = 0)':34}{_f(m['bot_sharpe']) if m['bot_sharpe'] is not None else 'n/d':>14}"
        f"{_f(m['bench_sharpe']) if m['bench_sharpe'] is not None else 'n/d':>16}",
        "",
        "Costi del bot",
        f"  Commissioni pagate:       {_f(m['commissions'])} {cur}",
        f"  Slippage stimato:         {_f(m['slippage'])} {cur}",
        f"  Costo API Claude:         {_f(m['api_cost'])} USD in {m['agent_runs']} esecuzioni",
        "",
        "Operatività",
        f"  Operazioni totali:        {m['n_trades']}  (vendite chiuse: {m['n_closed']}, "
        f"in guadagno: {_f(m['win_rate_pct'], '%', 0) if m['win_rate_pct'] is not None else 'n/d'})",
        f"  P&L realizzato:           {_f(m['realized_pnl'])} {cur}",
        f"  Turnover:                 {_f(m['turnover_pct'], '%', 0)} del capitale iniziale",
    ]
    if not m["benchmark_funded"]:
        lines += ["", "Nota: il benchmark non è ancora stato acquistato (serve un'esecuzione a mercato aperto)."]
    if m["halted"]:
        lines += ["", f"ATTENZIONE: trading sospeso. {m['halt_reason']}"]
    lines += ["", "-" * 64, "Valutazione"]
    if m["trading_days_observed"] < MIN_TRADING_DAYS:
        lines.append(
            f"  Troppo presto per giudicare: {m['trading_days_observed']} giorni di borsa su un minimo "
            f"consigliato di {MIN_TRADING_DAYS} (meglio 6-12 mesi). Qualunque differenza ora è soprattutto fortuna."
        )
    lines.append(
        f"  Differenza netta bot vs benchmark: {excess:+.2f} punti percentuali "
        f"({'a favore del bot' if excess > 0 else 'a favore del benchmark'})."
    )
    if m["trading_days_observed"] >= MIN_TRADING_DAYS:
        if excess <= 0:
            lines.append("  Il bot non ha battuto il semplice acquisto del benchmark: con soldi veri non conviene.")
        elif m["bot_max_dd_pct"] < m["bench_max_dd_pct"] - 5:
            lines.append("  Il bot rende di più ma con perdite massime molto più profonde: valuta se il rischio è accettabile.")
        else:
            lines.append(
                "  Il bot ha fatto meglio del benchmark nel periodo. Prima di usare soldi veri, verifica che il risultato"
                " non dipenda da una o due operazioni fortunate (guarda trades.jsonl) e prosegui ancora la simulazione."
            )
    lines.append("  Stima fiscale semplificata (aliquota unica sul guadagno complessivo): chiedi conferma a un commercialista.")
    lines.append("=" * 64)
    return "\n".join(lines)

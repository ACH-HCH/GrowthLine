"""Comandi: init, run, status, report, resume, reset."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Config, load_config
from .engine import Engine, new_state
from .market import BaseMarket, YahooMarket
from .store import Store


def _agent_runs_today(cfg: Config, store: Store, now: datetime) -> int:
    """Esecuzioni di Claude nella giornata di borsa corrente (fuso orario del mercato)."""
    tz = ZoneInfo(cfg.market.timezone)
    today = now.astimezone(tz).date()
    return sum(
        1 for j in store.read_jsonl(store.journal_path)
        if datetime.fromisoformat(j["ts"]).astimezone(tz).date() == today
    )


def run_cycle(cfg: Config, market: BaseMarket, store: Store, *, use_agent: bool = True,
              now: datetime | None = None, agent_fn=None) -> dict:
    """Un ciclo completo. `agent_fn` permette di sostituire Claude nei test."""
    engine = Engine(cfg, market, store, now=now)
    engine.process_pending()
    engine.fund_benchmark()
    engine.check_drawdown()
    if not engine.state["halted"]:
        engine.apply_stop_losses()

    agent_result = None
    skip_reason = None
    if not use_agent:
        skip_reason = "agente disattivato (--no-agent)"
    elif engine.state["halted"]:
        skip_reason = "trading sospeso"
    elif not engine.market_open and not cfg.agent.run_when_market_closed:
        skip_reason = "mercato chiuso (run_when_market_closed = false)"
    elif _agent_runs_today(cfg, store, engine.now) >= cfg.agent.max_agent_runs_per_day:
        skip_reason = f"Claude ha già deciso oggi (max_agent_runs_per_day = {cfg.agent.max_agent_runs_per_day})"

    if skip_reason is None:
        journal = store.read_jsonl(store.journal_path)[-cfg.agent.journal_entries_in_prompt:]
        if agent_fn is None:
            from .agent import run_agent as agent_fn
        agent_result = agent_fn(engine, cfg, journal)
        engine.state["api_cost_usd"] += agent_result["api_cost_usd"]
        engine.state["agent_runs"] += 1
        store.append(store.journal_path, {
            "ts": engine.now.isoformat(),
            "market_open": engine.market_open,
            "orders": engine.order_results,
            "events": engine.events,
            **agent_result,
        })

    snap = engine.snapshot()
    engine.save()
    return {"snapshot": snap, "events": engine.events, "agent": agent_result, "skipped": skip_reason}


def _print_run(result: dict) -> None:
    s = result["snapshot"]
    print(f"[{s['ts'][:16]}] mercato {'aperto' if s['market_open'] else 'chiuso'}")
    for e in result["events"]:
        print("  •", e)
    if result["skipped"]:
        print(f"  Agente non eseguito: {result['skipped']}")
    if result["agent"]:
        a = result["agent"]
        print(f"\nResoconto di Claude ({', '.join(a['models'])}, fine: {a['stop']}, costo {a['api_cost_usd']:.3f} USD):")
        print(a["summary"] or "(nessun resoconto)")
    print(f"\nPatrimonio: {s['equity']:,.2f} | Cassa: {s['cash']:,.2f} | Benchmark: {s['benchmark_value']:,.2f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="papertrader", description="Simulatore di paper trading con Claude")
    parser.add_argument("--config", default="config.toml", help="file di configurazione (default: config.toml)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="crea un nuovo conto simulato")
    p_init.add_argument("--force", action="store_true", help="sovrascrive un conto esistente")
    p_run = sub.add_parser("run", help="esegue un ciclo (dati, ordini in coda, stop-loss, decisione di Claude)")
    p_run.add_argument("--no-agent", action="store_true", help="aggiorna prezzi e ordini senza chiamare Claude")
    sub.add_parser("status", help="mostra il portafoglio attuale")
    sub.add_parser("report", help="confronto con il benchmark e metriche")
    sub.add_parser("resume", help="riattiva il trading dopo una sospensione")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    store = Store(cfg.data_path)

    if args.cmd == "init":
        if store.exists() and not args.force:
            print(f"Esiste già un conto in {store.dir}. Usa --force per ricominciare da zero.")
            return 1
        store.reset()
        state = new_state(cfg, datetime.now(timezone.utc))
        store.save_state(state)
        print(f"Conto simulato creato: {state['initial_capital']:,.2f} {state['currency']} in {store.dir}")
        print(f"Il benchmark ({cfg.market.benchmark}) verrà acquistato alla prima esecuzione a mercato aperto.")
        return 0

    if args.cmd == "resume":
        state = store.load_state()
        state["halted"] = False
        state["halt_reason"] = None
        store.save_state(state)
        print("Trading riattivato. Il massimo di riferimento per il drawdown resta invariato.")
        return 0

    if args.cmd == "report":
        from .report import compute_metrics, format_report
        print(format_report(compute_metrics(cfg, store)))
        return 0

    market = YahooMarket(cfg.market)
    if args.cmd == "status":
        engine = Engine(cfg, market, store)
        print(json.dumps(engine.portfolio_summary(), indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "run":
        _print_run(run_cycle(cfg, market, store, use_agent=not args.no_agent))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

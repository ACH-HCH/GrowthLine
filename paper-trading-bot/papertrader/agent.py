"""Il "cervello": Claude riceve strumenti per leggere i dati e proporre ordini.

Ogni ordine passa dall'Engine, che applica i limiti di rischio. Il ciclo
si ferma anche se il costo delle chiamate API supera il tetto configurato.
"""

from __future__ import annotations

import json

import anthropic

from .config import Config
from .engine import Engine
from .market import MarketDataError, summarize_history

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_TICKER = {"type": "string", "description": "Simbolo Yahoo Finance, es. AAPL, SPY, ENI.MI"}

TOOLS = [
    {
        "name": "get_portfolio",
        "description": "Stato attuale del conto simulato: cassa, posizioni con prezzi correnti, P&L, ordini in coda e quanti ordini restano in questa esecuzione.",
        "strict": True,
        "input_schema": _schema({}, []),
    },
    {
        "name": "get_quote",
        "description": "Ultimo prezzo disponibile (può essere ritardato di ~15 minuti) e variazione giornaliera.",
        "strict": True,
        "input_schema": _schema({"ticker": _TICKER}, ["ticker"]),
    },
    {
        "name": "get_price_history",
        "description": "Riassunto dello storico giornaliero: rendimenti 1w/1m/3m/6m/1y, medie mobili 20/50/200, RSI, volatilità, drawdown, chiusure settimanali.",
        "strict": True,
        "input_schema": _schema(
            {"ticker": _TICKER, "period": {"type": "string", "enum": ["6mo", "1y", "2y", "5y"]}},
            ["ticker", "period"],
        ),
    },
    {
        "name": "get_fundamentals",
        "description": "Dati fondamentali da Yahoo Finance (P/E, crescita, margini, debito, dividendo, consenso analisti). Possono essere incompleti.",
        "strict": True,
        "input_schema": _schema({"ticker": _TICKER}, ["ticker"]),
    },
    {
        "name": "get_news",
        "description": "Titoli delle notizie recenti sul titolo (fonte Yahoo Finance).",
        "strict": True,
        "input_schema": _schema({"ticker": _TICKER}, ["ticker"]),
    },
    {
        "name": "place_order",
        "description": (
            "Invia un ordine a mercato al broker simulato. Il broker applica commissioni, slippage e i limiti "
            "di rischio: se l'ordine li viola viene rifiutato e la risposta indica la quantità massima ammessa. "
            "Se il mercato è chiuso l'ordine va in coda e viene eseguito alla prossima apertura."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "ticker": _TICKER,
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "quantity": {"type": "number", "description": "Numero di azioni (intero, salvo frazionarie abilitate)"},
                "reason": {"type": "string", "description": "Motivazione sintetica, verrà salvata nel registro"},
            },
            ["ticker", "side", "quantity", "reason"],
        ),
    },
]


def system_prompt(cfg: Config) -> str:
    r, c = cfg.risk, cfg.costs
    # Contenuto stabile tra un'esecuzione e l'altra: così la cache dei prompt funziona.
    return f"""Sei il gestore di un portafoglio azionario SIMULATO (paper trading). L'obiettivo della simulazione è capire
se una gestione basata su di te batte, al netto dei costi, il semplice acquisto e mantenimento del benchmark {cfg.market.benchmark}.
I dati di mercato sono reali, i soldi no, ma comportati esattamente come se lo fossero.

## Strategia assegnata
{cfg.agent.strategy.strip() or "Gestione prudente di lungo periodo, diversificata, con pochi movimenti."}

## Regole del conto (applicate dal broker, non aggirabili)
- Puoi comprare solo questi titoli: {", ".join(cfg.market.universe)}.
- Solo posizioni lunghe, niente leva né vendite allo scoperto.
- Massimo {r.max_order_pct}% del patrimonio per singolo ordine e {r.max_position_pct}% per singola posizione.
- Cassa minima: {r.min_cash_pct}% del patrimonio. Massimo {r.max_orders_per_run} ordini per esecuzione.
- Stop-loss automatico al -{r.stop_loss_pct}% dal prezzo medio di carico (0 = disattivato).
- Se il patrimonio scende del {r.max_drawdown_halt_pct}% dal massimo, il trading viene sospeso.
- Costi reali simulati: commissione {c.commission_per_share} per azione (minimo {c.commission_min}), slippage {c.slippage_bps} punti base per operazione,
  tassazione del {c.capital_gains_tax_pct}% sulle plusvalenze. Ogni operazione inutile riduce il rendimento.

## Come lavorare
- Usa gli strumenti per raccogliere i dati che ti servono prima di decidere; non inventare numeri.
- Non hai conoscenza dei prezzi attuali se non tramite gli strumenti: le tue conoscenze pregresse possono essere superate.
- Non fare nulla è spesso la scelta migliore. Opera solo con una ragione chiara e coerente con la strategia
  e con le decisioni prese nelle esecuzioni precedenti (le trovi nel registro): evita di cambiare idea senza nuovi fatti.
- Se un ordine viene rifiutato, leggi il motivo e decidi se riproporlo con una quantità ammessa.

## Risposta finale
Quando hai finito, scrivi in italiano un breve resoconto (massimo 200 parole): cosa hai fatto e perché,
cosa hai deciso di non fare e quali condizioni ti farebbero cambiare idea nelle prossime esecuzioni."""


def user_prompt(engine: Engine, journal: list[dict]) -> str:
    summary = engine.portfolio_summary()
    lines = [
        f"Data e ora (UTC): {engine.now.isoformat(timespec='minutes')}",
        f"Mercato: {'APERTO, gli ordini vengono eseguiti subito' if engine.market_open else 'CHIUSO, gli ordini andranno in coda'}",
        "",
        "Portafoglio attuale:",
        json.dumps(summary, ensure_ascii=False, indent=1),
    ]
    if engine.events:
        lines += ["", "Eventi automatici di questa esecuzione:", *[f"- {e}" for e in engine.events]]
    if journal:
        lines += ["", "Registro delle esecuzioni precedenti (dalla più vecchia):"]
        for j in journal:
            orders = "; ".join(
                f"{o.get('status')} {o.get('side')} {o.get('qty')} {o.get('ticker')}" for o in j.get("orders", [])
            ) or "nessun ordine"
            lines.append(f"- {j['ts'][:16]} | ordini: {orders}\n  {(j.get('summary') or '').strip()}")
    lines += ["", "Analizza la situazione e decidi se e come operare."]
    return "\n".join(lines)


def _execute_tool(engine: Engine, name: str, args: dict) -> tuple[str, bool]:
    try:
        if name == "get_portfolio":
            out = engine.portfolio_summary()
        elif name == "get_quote":
            q = engine.market.quote(args["ticker"].upper())
            out = {
                "ticker": q.ticker, "price": round(q.price, 4), "as_of_utc": q.timestamp.isoformat(timespec="minutes"),
                "previous_close": q.prev_close and round(q.prev_close, 4),
                "day_change_pct": q.day_change_pct and round(q.day_change_pct, 2),
            }
        elif name == "get_price_history":
            out = summarize_history(engine.market.history(args["ticker"].upper(), args["period"]))
        elif name == "get_fundamentals":
            out = engine.market.fundamentals(args["ticker"].upper()) or {"note": "Nessun dato disponibile"}
        elif name == "get_news":
            out = engine.market.news(args["ticker"].upper()) or {"note": "Nessuna notizia disponibile"}
        elif name == "place_order":
            out = engine.place_order(args["ticker"], args["side"], args["quantity"], args["reason"])
            return json.dumps(out, ensure_ascii=False, default=str), out["status"] == "rejected"
        else:
            return f"Strumento sconosciuto: {name}", True
    except MarketDataError as exc:
        return str(exc), True
    except (KeyError, TypeError, ValueError) as exc:
        return f"Argomenti non validi per {name}: {exc}", True
    return json.dumps(out, ensure_ascii=False, default=str), False


def _cost(usage, cfg: Config) -> float:
    pin = cfg.agent.input_price_per_mtok / 1e6
    pout = cfg.agent.output_price_per_mtok / 1e6
    g = lambda k: getattr(usage, k, 0) or 0  # noqa: E731
    return (
        g("input_tokens") * pin
        + g("cache_creation_input_tokens") * pin * 1.25
        + g("cache_read_input_tokens") * pin * 0.1
        + g("output_tokens") * pout
    )


def run_agent(engine: Engine, cfg: Config, journal: list[dict], client: anthropic.Anthropic | None = None) -> dict:
    client = client or anthropic.Anthropic()
    a = cfg.agent
    messages: list[dict] = [{"role": "user", "content": user_prompt(engine, journal)}]
    system = system_prompt(cfg)
    cost = 0.0
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    final_text = ""
    stop = "max_turns"
    served_by = set()

    for _ in range(a.max_turns):
        kwargs = dict(
            model=a.model,
            max_tokens=16000,
            system=system,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": a.effort},
            cache_control={"type": "ephemeral"},
        )
        try:
            if a.use_fallbacks:
                resp = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            stop = f"api_error {exc.status_code}: {exc.message}"
            break
        except anthropic.APIConnectionError as exc:
            stop = f"connection_error: {exc}"
            break

        served_by.add(resp.model)
        u = resp.usage
        cost += _cost(u, cfg)
        tokens["input"] += u.input_tokens or 0
        tokens["output"] += u.output_tokens or 0
        tokens["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        tokens["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        if text:
            final_text = text
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "pause_turn":
            continue
        if resp.stop_reason != "tool_use":
            stop = resp.stop_reason
            if resp.stop_reason == "refusal" and getattr(resp, "stop_details", None):
                stop += f" ({resp.stop_details.category})"
            break

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            content, is_error = _execute_tool(engine, block.name, dict(block.input or {}))
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})

        if cost >= a.max_api_cost_per_run_usd:
            stop = f"budget_exceeded ({cost:.2f} USD)"
            break

    return {
        "summary": final_text,
        "stop": stop,
        "api_cost_usd": round(cost, 4),
        "tokens": tokens,
        "models": sorted(served_by),
    }

"""Broker simulato e regole di rischio.

Tutto ciò che riguarda soldi (esecuzione, commissioni, slippage, limiti) è
deciso qui, nel codice. Il modello può solo *proporre* ordini: se un ordine
viola un limite viene rifiutato, qualunque cosa il modello pensi.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from .config import Config
from .market import BaseMarket, MarketDataError
from .store import Store

EPS = 1e-9


def commission(qty: float, price: float, cfg: Config) -> float:
    """Modello a tariffa per azione con minimo e tetto percentuale (stile IBKR Fixed)."""
    c = cfg.costs
    value = qty * price
    fee = max(c.commission_min, qty * c.commission_per_share)
    if c.commission_max_pct > 0:
        fee = min(fee, value * c.commission_max_pct / 100)
    return round(fee, 4)


def fill_price(market_price: float, side: str, cfg: Config) -> float:
    """Lo slippage peggiora sempre il prezzo: si compra un po' più caro, si vende un po' più basso."""
    s = cfg.costs.slippage_bps / 10_000
    return market_price * (1 + s) if side == "buy" else market_price * (1 - s)


def new_state(cfg: Config, now: datetime) -> dict:
    cap = float(cfg.account.initial_capital)
    return {
        "version": 1,
        "created_at": now.isoformat(),
        "currency": cfg.account.currency,
        "initial_capital": cap,
        "cash": cap,
        "positions": {},
        "pending_orders": [],
        "realized_pnl": 0.0,
        "total_commissions": 0.0,
        "total_slippage": 0.0,
        "api_cost_usd": 0.0,
        "agent_runs": 0,
        "peak_equity": cap,
        "halted": False,
        "halt_reason": None,
        "benchmark": {"ticker": cfg.market.benchmark, "qty": 0.0, "cash": cap, "funded_at": None},
    }


class Engine:
    def __init__(self, cfg: Config, market: BaseMarket, store: Store, now: datetime | None = None):
        self.cfg = cfg
        self.market = market
        self.store = store
        self.now = now or datetime.now(timezone.utc)
        self.state = store.load_state()
        self.market_open = market.is_open(self.now)
        self.orders_this_run = 0
        self.events: list[str] = []
        self.order_results: list[dict] = []

    # ------------------------------------------------------------------ prezzi
    def price(self, ticker: str) -> float:
        return self.market.quote(ticker).price

    def positions_value(self) -> float:
        return sum(p["qty"] * self.price(t) for t, p in self.state["positions"].items())

    def equity(self) -> float:
        return self.state["cash"] + self.positions_value()

    def benchmark_value(self) -> float:
        b = self.state["benchmark"]
        value = b["cash"]
        if b["qty"] > 0:
            value += b["qty"] * self.price(b["ticker"])
        return value

    # ---------------------------------------------------------------- utilità
    def _round_qty(self, qty: float) -> float:
        if self.cfg.costs.allow_fractional:
            return math.floor(qty * 1e4) / 1e4
        return float(math.floor(qty + EPS))

    def _pending_for(self, ticker: str, side: str) -> float:
        return sum(
            o["qty"] for o in self.state["pending_orders"] if o["ticker"] == ticker and o["side"] == side
        )

    def _pending_buy_cost(self, exclude_id: str | None = None) -> float:
        total = 0.0
        for o in self.state["pending_orders"]:
            if o["side"] == "buy" and o["id"] != exclude_id:
                p = fill_price(self.price(o["ticker"]), "buy", self.cfg)
                total += o["qty"] * p + commission(o["qty"], p, self.cfg)
        return total

    def max_buy_qty(self, ticker: str, exclude_pending_id: str | None = None) -> float:
        """Quantità massima acquistabile ora rispettando tutti i limiti."""
        r = self.cfg.risk
        eq = self.equity()
        p = fill_price(self.price(ticker), "buy", self.cfg)
        held = self.state["positions"].get(ticker, {}).get("qty", 0.0)
        pending = sum(
            o["qty"]
            for o in self.state["pending_orders"]
            if o["ticker"] == ticker and o["side"] == "buy" and o["id"] != exclude_pending_id
        )
        by_order = eq * r.max_order_pct / 100 / p
        by_position = eq * r.max_position_pct / 100 / p - held - pending
        spendable = self.state["cash"] - self._pending_buy_cost(exclude_pending_id) - eq * r.min_cash_pct / 100
        # stima prudente: la commissione minima ridotta dalla cassa disponibile
        by_cash = (spendable - self.cfg.costs.commission_min) / p
        return max(0.0, self._round_qty(min(by_order, by_position, by_cash)))

    # -------------------------------------------------------------- controlli
    def validate(self, ticker: str, side: str, qty: float, *, count_order: bool = True,
                 pending_id: str | None = None) -> str | None:
        """Restituisce None se l'ordine è ammesso, altrimenti il motivo del rifiuto."""
        r = self.cfg.risk
        if side not in ("buy", "sell"):
            return "side deve essere 'buy' o 'sell'"
        if not (qty > 0) or math.isinf(qty):
            return "La quantità deve essere positiva"
        if not self.cfg.costs.allow_fractional and abs(qty - round(qty)) > EPS:
            return "Sono ammesse solo quantità intere (allow_fractional = false)"
        if count_order and self.orders_this_run >= r.max_orders_per_run:
            return f"Limite di {r.max_orders_per_run} ordini per esecuzione raggiunto"
        if side == "buy" and ticker not in self.cfg.market.universe:
            return f"{ticker} non è nell'universo consentito: {', '.join(self.cfg.market.universe)}"
        try:
            self.price(ticker)
        except MarketDataError as exc:
            return f"Prezzo non disponibile: {exc}"

        if side == "sell":
            held = self.state["positions"].get(ticker, {}).get("qty", 0.0)
            other_pending = sum(
                o["qty"] for o in self.state["pending_orders"]
                if o["ticker"] == ticker and o["side"] == "sell" and o["id"] != pending_id
            )
            available = held - other_pending
            if qty > available + EPS:
                return f"Vendita allo scoperto non consentita: disponibili {available:g} {ticker}"
            return None

        if self.state["halted"]:
            return f"Trading sospeso: {self.state['halt_reason']}"
        max_qty = self.max_buy_qty(ticker, exclude_pending_id=pending_id)
        if qty > max_qty + EPS:
            return (
                f"L'ordine supera i limiti di rischio (max {r.max_order_pct}% del conto per ordine, "
                f"{r.max_position_pct}% per posizione, cassa minima {r.min_cash_pct}%). "
                f"Quantità massima acquistabile ora: {max_qty:g}"
            )
        return None

    # ------------------------------------------------------------- esecuzione
    def _execute(self, ticker: str, side: str, qty: float, reason: str, source: str) -> dict:
        mkt = self.price(ticker)
        px = fill_price(mkt, side, self.cfg)
        fee = commission(qty, px, self.cfg)
        slippage_cost = abs(px - mkt) * qty
        positions = self.state["positions"]
        realized = None
        if side == "buy":
            pos = positions.setdefault(ticker, {"qty": 0.0, "avg_cost": 0.0, "opened_at": self.now.isoformat()})
            cost_basis = pos["qty"] * pos["avg_cost"] + qty * px + fee  # commissioni nel costo fiscale
            pos["qty"] += qty
            pos["avg_cost"] = cost_basis / pos["qty"]
            self.state["cash"] -= qty * px + fee
        else:
            pos = positions[ticker]
            realized = (px - pos["avg_cost"]) * qty - fee
            pos["qty"] -= qty
            self.state["cash"] += qty * px - fee
            self.state["realized_pnl"] += realized
            if pos["qty"] <= EPS:
                del positions[ticker]
        self.state["total_commissions"] += fee
        self.state["total_slippage"] += slippage_cost
        fill = {
            "ts": self.now.isoformat(),
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "market_price": round(mkt, 4),
            "fill_price": round(px, 4),
            "commission": fee,
            "value": round(qty * px, 2),
            "realized_pnl": round(realized, 2) if realized is not None else None,
            "source": source,
            "reason": reason,
        }
        self.store.append(self.store.trades_path, fill)
        self.events.append(
            f"ESEGUITO {side.upper()} {qty:g} {ticker} @ {px:.2f} (commissione {fee:.2f}) [{source}]"
        )
        return fill

    def place_order(self, ticker: str, side: str, qty: float, reason: str, source: str = "agent",
                    count_order: bool = True) -> dict:
        ticker = ticker.strip().upper()
        qty = float(qty)
        err = self.validate(ticker, side, qty, count_order=count_order)
        if err:
            result = {"status": "rejected", "ticker": ticker, "side": side, "qty": qty, "error": err}
            self.events.append(f"RIFIUTATO {side} {qty:g} {ticker}: {err}")
        elif self.market_open:
            self.orders_this_run += int(count_order)
            result = {"status": "filled", **self._execute(ticker, side, qty, reason, source)}
        else:
            self.orders_this_run += int(count_order)
            order = {
                "id": uuid.uuid4().hex[:8], "ticker": ticker, "side": side, "qty": qty,
                "reason": reason, "source": source, "created_at": self.now.isoformat(),
            }
            self.state["pending_orders"].append(order)
            self.events.append(f"IN CODA {side} {qty:g} {ticker}: mercato chiuso, eseguito alla prossima apertura")
            result = {
                "status": "queued", **order,
                "note": "Mercato chiuso: sarà eseguito al prezzo di mercato alla prossima esecuzione a mercato aperto.",
            }
        self.order_results.append(result)
        return result

    def process_pending(self) -> None:
        if not self.market_open or not self.state["pending_orders"]:
            return
        queue = list(self.state["pending_orders"])
        # prima le vendite, così liberano cassa per gli acquisti
        queue.sort(key=lambda o: 0 if o["side"] == "sell" else 1)
        for order in queue:
            err = self.validate(order["ticker"], order["side"], order["qty"], count_order=False,
                                pending_id=order["id"])
            self.state["pending_orders"] = [o for o in self.state["pending_orders"] if o["id"] != order["id"]]
            if err:
                self.events.append(f"ANNULLATO ordine in coda {order['side']} {order['qty']:g} {order['ticker']}: {err}")
                continue
            self._execute(order["ticker"], order["side"], order["qty"], order["reason"], order["source"] + "/coda")

    def fund_benchmark(self) -> None:
        """Il benchmark compra il suo ETF con lo stesso capitale e gli stessi costi, e poi lo tiene."""
        b = self.state["benchmark"]
        if b["funded_at"] or not self.market_open:
            return
        px = fill_price(self.price(b["ticker"]), "buy", self.cfg)
        qty = self._round_qty((b["cash"] - self.cfg.costs.commission_min * 2) / px)
        while qty > 0 and qty * px + commission(qty, px, self.cfg) > b["cash"]:
            qty -= 1 if not self.cfg.costs.allow_fractional else 0.0001
        if qty <= 0:
            return
        b["cash"] -= qty * px + commission(qty, px, self.cfg)
        b["qty"] = qty
        b["funded_at"] = self.now.isoformat()
        self.events.append(f"Benchmark: acquistate {qty:g} {b['ticker']} @ {px:.2f}")

    def apply_stop_losses(self) -> None:
        sl = self.cfg.risk.stop_loss_pct
        if sl <= 0:
            return
        for ticker, pos in list(self.state["positions"].items()):
            try:
                px = self.price(ticker)
            except MarketDataError:
                continue
            if px <= pos["avg_cost"] * (1 - sl / 100):
                qty = pos["qty"] - self._pending_for(ticker, "sell")
                if qty <= EPS:
                    continue
                self.events.append(f"STOP-LOSS su {ticker}: prezzo {px:.2f} vs costo medio {pos['avg_cost']:.2f}")
                # lo stop-loss non consuma il limite di ordini del modello
                self.place_order(ticker, "sell", qty, f"Stop-loss automatico al -{sl}%",
                                 source="stop-loss", count_order=False)

    def check_drawdown(self) -> None:
        eq = self.equity()
        self.state["peak_equity"] = max(self.state["peak_equity"], eq)
        limit = self.cfg.risk.max_drawdown_halt_pct
        if limit <= 0 or self.state["halted"]:
            return
        dd = (1 - eq / self.state["peak_equity"]) * 100
        if dd >= limit:
            self.state["halted"] = True
            self.state["halt_reason"] = (
                f"Drawdown del {dd:.1f}% dal massimo (limite {limit}%) il {self.now.date()}. "
                "Analizza cosa è successo prima di riattivare con: python -m papertrader resume"
            )
            cancelled = [o for o in self.state["pending_orders"] if o["side"] == "buy"]
            self.state["pending_orders"] = [o for o in self.state["pending_orders"] if o["side"] != "buy"]
            self.events.append(f"TRADING SOSPESO: {self.state['halt_reason']}")
            if cancelled:
                self.events.append(f"Annullati {len(cancelled)} acquisti in coda")

    # ------------------------------------------------------------ resoconti
    def portfolio_summary(self) -> dict:
        eq = self.equity()
        positions = []
        for t, p in sorted(self.state["positions"].items()):
            px = self.price(t)
            value = p["qty"] * px
            positions.append({
                "ticker": t,
                "qty": p["qty"],
                "avg_cost": round(p["avg_cost"], 2),
                "price": round(px, 2),
                "value": round(value, 2),
                "weight_pct": round(value / eq * 100, 1),
                "unrealized_pnl": round((px - p["avg_cost"]) * p["qty"], 2),
                "unrealized_pnl_pct": round((px / p["avg_cost"] - 1) * 100, 2),
                "opened_at": p["opened_at"][:10],
            })
        return {
            "currency": self.state["currency"],
            "equity": round(eq, 2),
            "cash": round(self.state["cash"], 2),
            "cash_pct": round(self.state["cash"] / eq * 100, 1),
            "initial_capital": self.state["initial_capital"],
            "total_return_pct": round((eq / self.state["initial_capital"] - 1) * 100, 2),
            "realized_pnl": round(self.state["realized_pnl"], 2),
            "total_commissions_paid": round(self.state["total_commissions"], 2),
            "positions": positions,
            "pending_orders": self.state["pending_orders"],
            "market_open": self.market_open,
            "orders_left_this_run": max(0, self.cfg.risk.max_orders_per_run - self.orders_this_run),
            "halted": self.state["halted"],
        }

    def snapshot(self) -> dict:
        snap = {
            "ts": self.now.isoformat(),
            "equity": round(self.equity(), 2),
            "cash": round(self.state["cash"], 2),
            "benchmark_value": round(self.benchmark_value(), 2),
            "api_cost_usd": round(self.state["api_cost_usd"], 4),
            "market_open": self.market_open,
        }
        self.store.append(self.store.equity_path, snap)
        return snap

    def save(self) -> None:
        self.store.save_state(self.state)

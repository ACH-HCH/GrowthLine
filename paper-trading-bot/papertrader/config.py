"""Caricamento e validazione della configurazione (file TOML)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class AccountConfig:
    initial_capital: float = 10_000.0
    currency: str = "USD"
    data_dir: str = "data"


@dataclass
class MarketConfig:
    timezone: str = "America/New_York"
    open: str = "09:30"
    close: str = "16:00"
    benchmark: str = "SPY"
    # Un prezzo più vecchio di così durante l'orario di borsa = mercato chiuso (es. festività).
    max_quote_age_minutes: int = 30
    universe: list[str] = field(default_factory=lambda: ["SPY"])


@dataclass
class CostsConfig:
    commission_per_share: float = 0.005
    commission_min: float = 1.0
    commission_max_pct: float = 1.0
    slippage_bps: float = 5.0
    allow_fractional: bool = False
    capital_gains_tax_pct: float = 26.0


@dataclass
class RiskConfig:
    max_position_pct: float = 20.0
    max_order_pct: float = 10.0
    min_cash_pct: float = 5.0
    max_orders_per_run: int = 5
    stop_loss_pct: float = 15.0
    max_drawdown_halt_pct: float = 20.0


@dataclass
class AgentConfig:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_turns: int = 25
    use_fallbacks: bool = True
    input_price_per_mtok: float = 4.0
    output_price_per_mtok: float = 20.0
    max_api_cost_per_run_usd: float = 1.0
    journal_entries_in_prompt: int = 5
    run_when_market_closed: bool = False
    strategy: str = ""


@dataclass
class Config:
    account: AccountConfig
    market: MarketConfig
    costs: CostsConfig
    risk: RiskConfig
    agent: AgentConfig
    base_dir: Path = Path(".")

    @property
    def data_path(self) -> Path:
        p = Path(self.account.data_dir)
        return p if p.is_absolute() else self.base_dir / p


def _build(cls, raw: dict, section: str):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Chiavi sconosciute in [{section}]: {', '.join(sorted(unknown))}")
    return cls(**raw)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    sections = {"account", "market", "costs", "risk", "agent"}
    unknown = set(raw) - sections
    if unknown:
        raise ValueError(f"Sezioni sconosciute nella configurazione: {', '.join(sorted(unknown))}")
    cfg = Config(
        account=_build(AccountConfig, raw.get("account", {}), "account"),
        market=_build(MarketConfig, raw.get("market", {}), "market"),
        costs=_build(CostsConfig, raw.get("costs", {}), "costs"),
        risk=_build(RiskConfig, raw.get("risk", {}), "risk"),
        agent=_build(AgentConfig, raw.get("agent", {}), "agent"),
        base_dir=path.parent.resolve(),
    )
    cfg.market.universe = [t.strip().upper() for t in cfg.market.universe]
    cfg.market.benchmark = cfg.market.benchmark.strip().upper()
    if cfg.account.initial_capital <= 0:
        raise ValueError("initial_capital deve essere positivo")
    if cfg.agent.effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("agent.effort deve essere low, medium, high, xhigh o max")
    return cfg

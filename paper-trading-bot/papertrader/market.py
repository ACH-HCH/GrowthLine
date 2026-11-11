"""Dati di mercato reali (Yahoo Finance tramite yfinance) e indicatori tecnici.

Nota: i prezzi gratuiti di Yahoo possono essere ritardati di circa 15 minuti.
Per una simulazione giornaliera va bene; per il trading intraday no.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from .config import MarketConfig


@dataclass
class Quote:
    ticker: str
    price: float
    timestamp: datetime  # UTC, timezone-aware
    prev_close: float | None = None

    @property
    def day_change_pct(self) -> float | None:
        if not self.prev_close:
            return None
        return (self.price / self.prev_close - 1) * 100


class MarketDataError(RuntimeError):
    pass


class BaseMarket:
    """Interfaccia comune; i test usano una versione finta con prezzi controllati."""

    def __init__(self, cfg: MarketConfig):
        self.cfg = cfg

    def quote(self, ticker: str) -> Quote:  # pragma: no cover - interfaccia
        raise NotImplementedError

    def history(self, ticker: str, period: str = "1y") -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    def fundamentals(self, ticker: str) -> dict:  # pragma: no cover
        raise NotImplementedError

    def news(self, ticker: str, limit: int = 8) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def is_open(self, now: datetime) -> bool:
        """Aperto se è giorno feriale, in orario, e il benchmark ha un prezzo recente.

        L'ultimo controllo serve a riconoscere le festività senza un calendario.
        """
        tz = ZoneInfo(self.cfg.timezone)
        local = now.astimezone(tz)
        if local.weekday() >= 5:
            return False
        open_t = time.fromisoformat(self.cfg.open)
        close_t = time.fromisoformat(self.cfg.close)
        if not (open_t <= local.time() < close_t):
            return False
        try:
            q = self.quote(self.cfg.benchmark)
        except MarketDataError:
            return False
        age = now - q.timestamp
        # Il ritardo dei dati gratuiti (~15 min) è incluso nella tolleranza.
        return age <= timedelta(minutes=self.cfg.max_quote_age_minutes)


class YahooMarket(BaseMarket):
    def __init__(self, cfg: MarketConfig):
        super().__init__(cfg)
        import yfinance as yf  # import tardivo: i test non ne hanno bisogno

        self._yf = yf
        self._quotes: dict[str, Quote] = {}

    def _ticker(self, ticker: str):
        return self._yf.Ticker(ticker)

    def quote(self, ticker: str) -> Quote:
        ticker = ticker.upper()
        if ticker in self._quotes:
            return self._quotes[ticker]
        t = self._ticker(ticker)
        try:
            intraday = t.history(period="5d", interval="1m", prepost=False, auto_adjust=False)
            daily = t.history(period="10d", interval="1d", auto_adjust=False)
        except Exception as exc:  # yfinance solleva eccezioni di vario tipo
            raise MarketDataError(f"Errore nel recupero di {ticker}: {exc}") from exc
        source = intraday if not intraday.empty else daily
        source = source.dropna(subset=["Close"]) if not source.empty else source
        if source.empty:
            raise MarketDataError(f"Nessun prezzo disponibile per {ticker} (ticker errato o delistato?)")
        price = float(source["Close"].iloc[-1])
        ts = source.index[-1].to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        prev_close = None
        daily = daily.dropna(subset=["Close"]) if not daily.empty else daily
        if len(daily) >= 2:
            last_day = daily.index[-1].date()
            prev_close = float(
                daily["Close"].iloc[-2] if last_day == source.index[-1].date() else daily["Close"].iloc[-1]
            )
        q = Quote(ticker=ticker, price=price, timestamp=ts, prev_close=prev_close)
        self._quotes[ticker] = q
        return q

    def history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        try:
            df = self._ticker(ticker.upper()).history(period=period, interval="1d", auto_adjust=True)
        except Exception as exc:
            raise MarketDataError(f"Errore nello storico di {ticker}: {exc}") from exc
        if df.empty:
            raise MarketDataError(f"Nessuno storico per {ticker}")
        return df.dropna(subset=["Close"])

    def fundamentals(self, ticker: str) -> dict:
        try:
            info = self._ticker(ticker.upper()).info or {}
        except Exception as exc:
            raise MarketDataError(f"Errore nei fondamentali di {ticker}: {exc}") from exc
        keys = [
            "longName", "quoteType", "sector", "industry", "currency", "marketCap",
            "trailingPE", "forwardPE", "priceToBook", "pegRatio", "dividendYield",
            "profitMargins", "revenueGrowth", "earningsGrowth", "returnOnEquity",
            "debtToEquity", "freeCashflow", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
            "recommendationKey", "targetMeanPrice", "numberOfAnalystOpinions",
            "totalAssets", "expenseRatio", "category",
        ]
        return {k: info[k] for k in keys if info.get(k) not in (None, "", "N/A")}

    def news(self, ticker: str, limit: int = 8) -> list[dict]:
        try:
            raw = self._ticker(ticker.upper()).news or []
        except Exception as exc:
            raise MarketDataError(f"Errore nelle notizie di {ticker}: {exc}") from exc
        out = []
        for item in raw[:limit]:
            c = item.get("content", item)  # yfinance ha cambiato formato nel tempo
            provider = c.get("provider") or {}
            published = c.get("pubDate") or c.get("providerPublishTime")
            if isinstance(published, (int, float)):
                published = datetime.fromtimestamp(published, timezone.utc).isoformat()
            out.append(
                {
                    "title": c.get("title"),
                    "publisher": provider.get("displayName") if isinstance(provider, dict) else c.get("publisher"),
                    "published": published,
                    "summary": (c.get("summary") or "")[:400] or None,
                }
            )
        return [n for n in out if n["title"]]


def _ret(close: pd.Series, days: int) -> float | None:
    if len(close) <= days:
        return None
    return round((close.iloc[-1] / close.iloc[-1 - days] - 1) * 100, 2)


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) <= period:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    if loss == 0:
        return 100.0
    return round(100 - 100 / (1 + gain / loss), 1)


def summarize_history(df: pd.DataFrame) -> dict:
    """Riassunto compatto dello storico giornaliero, da passare al modello."""
    close = df["Close"].astype(float)
    last = float(close.iloc[-1])

    def sma(n: int) -> float | None:
        return round(float(close.tail(n).mean()), 2) if len(close) >= n else None

    daily_ret = close.pct_change().dropna()
    vol = daily_ret.tail(63).std() * math.sqrt(252) * 100 if len(daily_ret) >= 20 else None
    running_max = close.cummax()
    max_dd = float(((close / running_max) - 1).min() * 100)
    weekly = close.iloc[::-5][:12][::-1]  # ~ultime 12 settimane, una chiusura a settimana
    return {
        "last_close": round(last, 2),
        "as_of": str(close.index[-1].date()),
        "return_pct": {
            "1w": _ret(close, 5), "1m": _ret(close, 21), "3m": _ret(close, 63),
            "6m": _ret(close, 126), "1y": _ret(close, 252),
        },
        "sma": {"20d": sma(20), "50d": sma(50), "200d": sma(200)},
        "rsi_14": _rsi(close),
        "volatility_3m_annualized_pct": round(float(vol), 1) if vol is not None else None,
        "high_period": round(float(close.max()), 2),
        "low_period": round(float(close.min()), 2),
        "max_drawdown_period_pct": round(max_dd, 2),
        "avg_volume_20d": int(df["Volume"].tail(20).mean()) if "Volume" in df else None,
        "weekly_closes": [round(float(v), 2) for v in weekly],
        "last_10_daily_closes": [round(float(v), 2) for v in close.tail(10)],
    }

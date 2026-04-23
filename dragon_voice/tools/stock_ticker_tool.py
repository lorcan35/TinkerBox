"""
StockTickerTool — real-time stock/ETF quote.

Designed by TinkerClaw (Gemini 3 Flash Preview) on 2026-04-22 during
the "max-capabilities" long session, then installed on Dragon by the
user (Emile) and exercised end-to-end.

Uses Stooq CSV (no auth, usually reachable) as primary, Yahoo Finance
chart API as fallback. Yahoo frequently 429s Dragon's static IP — Stooq
has been stable.
"""

import csv
import io
import logging

import aiohttp

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)

STOOQ = "https://stooq.com/q/l/?s={symbol}.us&f=sd2t2ohlcv&h&e=csv"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
UA = (
    "Mozilla/5.0 (X11; Linux aarch64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class StockTickerTool(Tool):
    """Fetch the current price + daily change for a US ticker."""

    @property
    def name(self) -> str:
        return "stock_ticker"

    @property
    def description(self) -> str:
        return (
            "Get the current price and daily change for a US stock or ETF ticker "
            "(e.g. AAPL, TSLA, SPY, MSFT). Returns price, open, high, low, percent change."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "US ticker symbol, e.g. AAPL, MSFT, TSLA, SPY",
                },
                "currency": {
                    "type": "string",
                    "description": "ISO currency code, defaults to USD",
                    "default": "USD",
                },
            },
            "required": ["symbol"],
        }

    async def execute(self, args: dict) -> dict:
        symbol = (args.get("symbol") or "").upper().strip()
        currency = (args.get("currency") or "USD").upper().strip()
        if not symbol or not symbol.replace(".", "").replace("-", "").isalnum():
            return {"error": "invalid symbol", "symbol": symbol}

        timeout = aiohttp.ClientTimeout(total=6)
        headers = {"User-Agent": UA, "Accept": "*/*"}

        # Primary: Stooq CSV (no rate limiting on Dragon's IP)
        try:
            url = STOOQ.format(symbol=symbol.lower())
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                async with s.get(url) as r:
                    if r.status == 200:
                        row = self._parse_stooq(await r.text())
                        if row:
                            price = row["close"]
                            prev = row["open"]
                            change = price - prev
                            pct = (change / prev * 100.0) if prev else 0.0
                            arrow = "↑" if change > 0 else ("↓" if change < 0 else "→")
                            summary = (
                                f"{symbol} {arrow} {price:.2f} {currency} "
                                f"({change:+.2f}, {pct:+.2f}% vs open)"
                            )
                            logger.info("stock_ticker stooq: %s", summary)
                            return {
                                "symbol": symbol,
                                "source": "stooq",
                                "price": round(price, 4),
                                "open": round(row["open"], 4),
                                "high": round(row["high"], 4),
                                "low": round(row["low"], 4),
                                "volume": row["volume"],
                                "as_of": f"{row['date']} {row['time']}",
                                "currency": currency,
                                "change": round(change, 4),
                                "change_pct": round(pct, 4),
                                "summary": summary,
                            }
        except Exception as e:
            logger.warning("stooq fetch failed for %s: %s", symbol, e)

        # Fallback: Yahoo Finance
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                async with s.get(YAHOO.format(symbol=symbol)) as r:
                    if r.status != 200:
                        return {"error": f"both sources unreachable (yahoo http {r.status})", "symbol": symbol}
                    payload = await r.json()
            meta = payload["chart"]["result"][0]["meta"]
            price = float(meta.get("regularMarketPrice") or 0.0)
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0.0)
            change = price - prev if prev else 0.0
            pct = (change / prev * 100.0) if prev else 0.0
            arrow = "↑" if change > 0 else ("↓" if change < 0 else "→")
            return {
                "symbol": symbol,
                "source": "yahoo",
                "price": round(price, 4),
                "currency": meta.get("currency", currency),
                "change": round(change, 4),
                "change_pct": round(pct, 4),
                "exchange": meta.get("exchangeName", ""),
                "summary": f"{symbol} {arrow} {price:.2f} ({change:+.2f}, {pct:+.2f}%)",
            }
        except Exception as e:
            logger.exception("yahoo fallback failed for %s", symbol)
            return {"error": f"fetch failed: {e}", "symbol": symbol}

    @staticmethod
    def _parse_stooq(text: str):
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            try:
                return {
                    "date": row["Date"],
                    "time": row["Time"],
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]),
                }
            except (KeyError, ValueError):
                return None
        return None

"""
cipher_lens/engine/scoring.py
=============================
Cipher Lens scoring engine — analyzes individual tickers against a structured
framework derived from Cipher Sentinel v1.5.

This module is intentionally standalone:
  - No dependencies on Cipher Suite paths or state
  - Inputs: a ticker symbol
  - Outputs: a structured score result with category-level breakdown

DESIGN NOTE — IP PROTECTION:
The scoring rules and thresholds are kept INTERNAL. The output exposes only:
  - Final rating (green/yellow/red)
  - High-level category breakdown (e.g., "Trend signals: 4 of 5 passed")
  - The actual technical readings (price, RSI, MAs, etc.) — these are facts, not IP
What is NEVER exposed in output:
  - Specific threshold values (e.g., "RSI band 40-65")
  - Exact weights between Cat A and Cat B
  - The full list of conditions or signals being checked

The user sees CATEGORIES of signals, never the actual rules that drive the score.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import yfinance as yf

log = logging.getLogger("cipher_lens.scoring")

# ============================================================
# INTERNAL CONSTANTS — Do not expose in API output
# ============================================================

# Cat A 12-condition thresholds (from Sentinel v1.5)
_CAT_A_RSI_LOWER = 40
_CAT_A_RSI_UPPER = 65
_CAT_A_MIN_UPSIDE = 0.10            # 10% to analyst target
_CAT_A_PCT_BELOW_200MA = 0.10
_CAT_A_PCT_OFF_52WK_HIGH = 0.05
_CAT_A_MIN_BUY_RATIO = 0.60

# Cat B signal thresholds
_CAT_B_ENTRY_THRESHOLD = 9          # 9+ of 14 = Entry Permitted
_CAT_B_HIGH_THRESHOLD = 12          # 12+ of 14 = High Conviction

# Earnings blackout
_EARNINGS_BLACKOUT_DAYS = 21

# Sector momentum thresholds (vs SPY)
_SECTOR_HARD_HEADWIND_PCT = -5.0    # >5% below SPY = hard headwind
_SECTOR_MILD_HEADWIND_PCT = 0.0     # 0% to -5% = mild headwind

# Rating thresholds (these ARE exposed indirectly via the green/yellow/red mapping)
_RATING_GREEN_MIN_SCORE = 9         # cat_b 9+/14 OR cat_a 10+/12
_RATING_GREEN_MIN_CAT_A = 10
_RATING_YELLOW_MIN_SCORE = 6        # cat_b 6-8/14
_RATING_YELLOW_MIN_CAT_A = 7

# Public sector → ETF mapping (this can be exposed; standard mapping)
SECTOR_ETFS = {
    "Technology": "SMH", "Tech": "SMH",
    "Financial Services": "XLF", "Financials": "XLF",
    "Energy": "XLE",
    "Consumer Cyclical": "XLY", "Consumer Defensive": "XLY", "Consumer": "XLY",
    "Real Estate": "XLRE", "REITs": "XLRE",
    "Healthcare": "XLV",
    "Industrials": "XAR", "Defense": "XAR",
    "Communication Services": "XLC",
    "Basic Materials": "XLB", "Utilities": "XLU",
    "Travel": "JETS",
}
BENCHMARK = "SPY"


# ============================================================
# Data structures — what's exposed to the API
# ============================================================

@dataclass
class CategoryBreakdown:
    """High-level signal category result. Exposed in output.
    Shows passed/total for a category WITHOUT exposing which specific rules were checked."""
    name: str
    passed: int
    total: int
    summary: str  # One-line plain-English summary

@dataclass
class ScoreResult:
    """The full scoring result for one ticker. This is what the API returns."""
    ticker: str
    rating: str  # 'green' | 'yellow' | 'red' | 'insufficient_data'
    headline: str  # One-line "why this rating"

    # The actual technical readings (facts, not IP)
    price: Optional[float] = None
    price_date: Optional[str] = None
    daily_pct: Optional[float] = None
    rsi: Optional[float] = None
    rsi_2d_ago: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    price_vs_sma50_pct: Optional[float] = None
    price_vs_sma200_pct: Optional[float] = None
    high_52w: Optional[float] = None
    pct_off_52w_high: Optional[float] = None
    volume_today: Optional[int] = None
    volume_10d_avg: Optional[float] = None
    volume_ratio: Optional[float] = None
    market_cap: Optional[float] = None
    sector: Optional[str] = None
    sector_etf: Optional[str] = None
    sector_vs_spy_3wk: Optional[float] = None
    sector_regime: Optional[str] = None  # 'tailwind' | 'mild_headwind' | 'hard_headwind'
    earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    in_earnings_blackout: bool = False

    # Category-level breakdown (NOT specific rules)
    categories: list = field(default_factory=list)

    # Notes / flags
    notes: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    # Run metadata
    scored_at_utc: str = ""


# ============================================================
# Data fetch helpers
# ============================================================

def _fetch_history(ticker: str, period: str = "1y"):
    """Fetch yfinance daily history. Returns DataFrame or None."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=period, auto_adjust=False)
        if hist is None or hist.empty:
            return None
        return hist
    except Exception as e:
        log.warning(f"{ticker}: history fetch failed: {e}")
        return None


def _fetch_info(ticker: str):
    """Fetch yfinance info (sector, market cap, analyst data, earnings)."""
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        return info
    except Exception as e:
        log.warning(f"{ticker}: info fetch failed: {e}")
        return {}


def _compute_rsi(closes, period: int = 14):
    """Compute RSI Wilder smoothing. Returns final value or None."""
    try:
        if len(closes) < period + 1:
            return None
        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi_series = 100 - (100 / (1 + rs))
        return rsi_series
    except Exception:
        return None


def _pct_change(series, n: int) -> Optional[float]:
    """Percent change over last n closes."""
    try:
        if series is None or len(series) < n + 1:
            return None
        return float((series.iloc[-1] / series.iloc[-(n + 1)] - 1) * 100)
    except Exception:
        return None


def _normalize_sector(sector_str: Optional[str]) -> Optional[str]:
    """Map yfinance sector strings to our normalized labels."""
    if not sector_str:
        return None
    if sector_str in SECTOR_ETFS:
        return sector_str
    # Common yfinance values
    mapping = {
        "Technology": "Technology",
        "Financial Services": "Financial Services",
        "Energy": "Energy",
        "Consumer Cyclical": "Consumer Cyclical",
        "Consumer Defensive": "Consumer Defensive",
        "Real Estate": "Real Estate",
        "Healthcare": "Healthcare",
        "Industrials": "Industrials",
        "Communication Services": "Communication Services",
        "Basic Materials": "Basic Materials",
        "Utilities": "Utilities",
    }
    return mapping.get(sector_str, sector_str)


# Cache for sector ETFs (refreshed once per scoring batch)
_SECTOR_CACHE = {}
_SECTOR_CACHE_EXPIRY = None


def _get_sector_data(force_refresh: bool = False):
    """Get sector ETF 3-week and 5-day performance. Cached for 30 minutes."""
    global _SECTOR_CACHE, _SECTOR_CACHE_EXPIRY
    now = datetime.now()
    if (not force_refresh and _SECTOR_CACHE
            and _SECTOR_CACHE_EXPIRY and now < _SECTOR_CACHE_EXPIRY):
        return _SECTOR_CACHE

    etfs_to_fetch = set(SECTOR_ETFS.values()) | {BENCHMARK}
    data = {}
    for etf in etfs_to_fetch:
        hist = _fetch_history(etf, period="3mo")
        if hist is None or hist.empty:
            data[etf] = {"pct_3wk": None, "pct_5d": None}
            continue
        closes = hist["Close"]
        data[etf] = {
            "pct_3wk": _pct_change(closes, 15),
            "pct_5d": _pct_change(closes, 5),
        }
    _SECTOR_CACHE = data
    _SECTOR_CACHE_EXPIRY = now + timedelta(minutes=30)
    return data


# ============================================================
# Scoring logic — internal, threshold-aware
# ============================================================

def _score_trend_signals(closes, current_price: float, sma_50: float, sma_200: float) -> tuple[int, int]:
    """Trend category: price relative to MAs, MA structure. Returns (passed, total)."""
    passed, total = 0, 0

    # Price above 50 MA
    total += 1
    if sma_50 and current_price > sma_50:
        passed += 1

    # Price above 200 MA
    total += 1
    if sma_200 and current_price > sma_200:
        passed += 1

    # 50 above 200 (golden cross structure)
    total += 1
    if sma_50 and sma_200 and sma_50 > sma_200:
        passed += 1

    # Recent uptrend (close vs close 15 days ago)
    total += 1
    pct_15d = _pct_change(closes, 15)
    if pct_15d is not None and pct_15d > 0:
        passed += 1

    # Within blackout zone of 52w high (close to high = strong trend)
    total += 1
    if len(closes) >= 252:
        high_52w = float(closes.tail(252).max())
        if high_52w > 0:
            pct_off = (high_52w - current_price) / high_52w
            if pct_off <= _CAT_A_PCT_OFF_52WK_HIGH:
                passed += 1

    return passed, total


def _score_momentum_signals(closes, rsi_series) -> tuple[int, int]:
    """Momentum category: RSI level, direction, oversold/overbought avoidance."""
    passed, total = 0, 0

    if rsi_series is None or len(rsi_series.dropna()) < 3:
        return 0, 3

    rsi_today = float(rsi_series.iloc[-1])
    rsi_2d_ago = float(rsi_series.iloc[-3])

    # RSI in healthy band
    total += 1
    if _CAT_A_RSI_LOWER <= rsi_today <= _CAT_A_RSI_UPPER:
        passed += 1

    # RSI not falling sharply
    total += 1
    if rsi_today >= rsi_2d_ago - 3:
        passed += 1

    # RSI not in deep oversold
    total += 1
    if rsi_today > 30:
        passed += 1

    return passed, total


def _score_volume_signals(volumes) -> tuple[int, int]:
    """Volume category: today vs 10-day, expansion on positive days."""
    passed, total = 0, 0

    if volumes is None or len(volumes) < 11:
        return 0, 2

    today_vol = float(volumes.iloc[-1])
    avg_10d = float(volumes.tail(10).mean())

    # Volume not collapsing
    total += 1
    if avg_10d > 0 and today_vol >= avg_10d * 0.5:
        passed += 1

    # Recent volume healthy (5-day avg vs 20-day avg)
    total += 1
    if len(volumes) >= 20:
        avg_5d = float(volumes.tail(5).mean())
        avg_20d = float(volumes.tail(20).mean())
        if avg_20d > 0 and avg_5d >= avg_20d * 0.8:
            passed += 1

    return passed, total


def _score_sector_signals(sector_etf: Optional[str], sector_data: dict) -> tuple[int, int, str]:
    """Sector category: tailwind, mild headwind, hard headwind.
    Returns (passed, total, regime_label)."""
    if not sector_etf or sector_etf not in sector_data:
        return 0, 2, "unknown"

    spy_3wk = sector_data.get(BENCHMARK, {}).get("pct_3wk")
    sector_3wk = sector_data[sector_etf].get("pct_3wk")

    if spy_3wk is None or sector_3wk is None:
        return 0, 2, "unknown"

    diff = sector_3wk - spy_3wk
    passed, total = 0, 2

    # Sector not in hard headwind
    if diff >= _SECTOR_HARD_HEADWIND_PCT:
        passed += 1

    # Sector at or above SPY
    if diff >= _SECTOR_MILD_HEADWIND_PCT:
        passed += 1

    if diff >= 0:
        regime = "tailwind"
    elif diff > _SECTOR_HARD_HEADWIND_PCT:
        regime = "mild_headwind"
    else:
        regime = "hard_headwind"

    return passed, total, regime


def _score_fundamentals_signals(info: dict, current_price: float) -> tuple[int, int]:
    """Fundamentals category: analyst target upside, buy ratio, market cap floor."""
    passed, total = 0, 0

    # Analyst target upside
    total += 1
    target = info.get("targetMeanPrice")
    if target and current_price and target > current_price:
        upside = (target - current_price) / current_price
        if upside >= _CAT_A_MIN_UPSIDE:
            passed += 1

    # Market cap above small-cap floor ($5B)
    total += 1
    mcap = info.get("marketCap")
    if mcap and mcap >= 5_000_000_000:
        passed += 1

    # Buy ratio from analyst recommendations
    total += 1
    rec_mean = info.get("recommendationMean")
    if rec_mean is not None and rec_mean <= 2.5:
        # 1=Strong Buy, 2=Buy, 3=Hold, 4=Sell, 5=Strong Sell
        passed += 1

    return passed, total


def _check_earnings_blackout(info: dict) -> tuple[Optional[str], Optional[int], bool]:
    """Check for upcoming earnings within blackout window."""
    earnings_ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
    if not earnings_ts:
        return None, None, False
    try:
        earnings_dt = datetime.fromtimestamp(earnings_ts)
        days_to = (earnings_dt - datetime.now()).days
        earnings_iso = earnings_dt.strftime("%Y-%m-%d")
        in_blackout = (0 <= days_to <= _EARNINGS_BLACKOUT_DAYS)
        return earnings_iso, days_to, in_blackout
    except Exception:
        return None, None, False


# ============================================================
# Public API — score_ticker
# ============================================================

def score_ticker(ticker: str, sector_data: Optional[dict] = None) -> ScoreResult:
    """Score a single ticker. Returns ScoreResult with rating + breakdown.
    sector_data can be pre-fetched to avoid redundant ETF fetches across batch scoring."""
    ticker = ticker.strip().upper()
    if not ticker:
        return ScoreResult(
            ticker="", rating="insufficient_data",
            headline="Empty ticker.", errors=["empty ticker symbol"]
        )

    result = ScoreResult(
        ticker=ticker, rating="insufficient_data",
        headline="Scoring...",
        scored_at_utc=datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    # Fetch
    hist = _fetch_history(ticker, period="1y")
    if hist is None or hist.empty:
        result.rating = "insufficient_data"
        result.headline = f"Could not fetch price data for {ticker}. Symbol may be invalid or data may be unavailable."
        result.errors.append("no price history")
        return result

    info = _fetch_info(ticker)
    if sector_data is None:
        sector_data = _get_sector_data()

    closes = hist["Close"]
    volumes = hist["Volume"]

    # Basic facts
    try:
        result.price = round(float(closes.iloc[-1]), 4)
        result.price_date = closes.index[-1].strftime("%Y-%m-%d")
        if len(closes) >= 2:
            prev = float(closes.iloc[-2])
            if prev > 0:
                result.daily_pct = round((result.price - prev) / prev * 100, 3)
    except Exception as e:
        result.errors.append(f"price extraction: {e}")

    # Technicals
    try:
        rsi_series = _compute_rsi(closes, period=14)
        if rsi_series is not None and len(rsi_series.dropna()) >= 3:
            result.rsi = round(float(rsi_series.iloc[-1]), 2)
            result.rsi_2d_ago = round(float(rsi_series.iloc[-3]), 2)
    except Exception as e:
        result.errors.append(f"RSI: {e}")
        rsi_series = None

    try:
        if len(closes) >= 50:
            result.sma_50 = round(float(closes.tail(50).mean()), 4)
            if result.price and result.sma_50:
                result.price_vs_sma50_pct = round(
                    (result.price - result.sma_50) / result.sma_50 * 100, 2)
        if len(closes) >= 200:
            result.sma_200 = round(float(closes.tail(200).mean()), 4)
            if result.price and result.sma_200:
                result.price_vs_sma200_pct = round(
                    (result.price - result.sma_200) / result.sma_200 * 100, 2)
    except Exception as e:
        result.errors.append(f"SMA: {e}")

    try:
        if len(closes) >= 252:
            result.high_52w = round(float(closes.tail(252).max()), 4)
            if result.price and result.high_52w:
                result.pct_off_52w_high = round(
                    (result.high_52w - result.price) / result.high_52w * 100, 2)
    except Exception:
        pass

    try:
        if len(volumes) >= 1:
            result.volume_today = int(volumes.iloc[-1])
        if len(volumes) >= 10:
            result.volume_10d_avg = round(float(volumes.tail(10).mean()), 0)
            if result.volume_10d_avg > 0:
                result.volume_ratio = round(result.volume_today / result.volume_10d_avg, 2)
    except Exception:
        pass

    # Sector + market cap
    raw_sector = info.get("sector")
    result.sector = _normalize_sector(raw_sector)
    if result.sector and result.sector in SECTOR_ETFS:
        result.sector_etf = SECTOR_ETFS[result.sector]

    result.market_cap = info.get("marketCap")

    # Earnings
    e_date, e_days, in_blackout = _check_earnings_blackout(info)
    result.earnings_date = e_date
    result.days_to_earnings = e_days
    result.in_earnings_blackout = in_blackout
    if in_blackout:
        result.notes.append(f"Earnings in {e_days} days — within 21-day blackout window")

    # ============================================================
    # CATEGORY SCORING
    # ============================================================
    cats = []

    # Trend
    p, t = _score_trend_signals(closes, result.price, result.sma_50, result.sma_200)
    cats.append(CategoryBreakdown(
        name="Trend",
        passed=p, total=t,
        summary=_trend_summary(p, t, result),
    ))

    # Momentum
    p, t = _score_momentum_signals(closes, rsi_series)
    cats.append(CategoryBreakdown(
        name="Momentum",
        passed=p, total=t,
        summary=_momentum_summary(p, t, result),
    ))

    # Volume
    p, t = _score_volume_signals(volumes)
    cats.append(CategoryBreakdown(
        name="Volume",
        passed=p, total=t,
        summary=_volume_summary(p, t, result),
    ))

    # Sector
    p, t, regime = _score_sector_signals(result.sector_etf, sector_data)
    result.sector_regime = regime
    if result.sector_etf and sector_data.get(result.sector_etf):
        spy = sector_data.get(BENCHMARK, {}).get("pct_3wk")
        sec = sector_data[result.sector_etf].get("pct_3wk")
        if spy is not None and sec is not None:
            result.sector_vs_spy_3wk = round(sec - spy, 2)
    cats.append(CategoryBreakdown(
        name="Sector",
        passed=p, total=t,
        summary=_sector_summary(p, t, regime, result),
    ))

    # Fundamentals
    p, t = _score_fundamentals_signals(info, result.price)
    cats.append(CategoryBreakdown(
        name="Fundamentals",
        passed=p, total=t,
        summary=_fundamentals_summary(p, t, result, info),
    ))

    result.categories = cats

    # ============================================================
    # FINAL RATING
    # ============================================================
    total_passed = sum(c.passed for c in cats)
    total_possible = sum(c.total for c in cats)

    if in_blackout:
        result.rating = "red"
        result.headline = f"Within earnings blackout ({e_days} days). Wait until after the report."
        return result

    if regime == "hard_headwind":
        result.rating = "red"
        result.headline = f"Sector ({result.sector}) is in hard headwind vs market — most stocks here struggle right now."
        return result

    pct_passed = (total_passed / total_possible * 100) if total_possible > 0 else 0

    if pct_passed >= 75:
        result.rating = "green"
        result.headline = f"Strong across multiple signal categories ({total_passed}/{total_possible} signals passed)."
    elif pct_passed >= 50:
        result.rating = "yellow"
        result.headline = f"Mixed signals ({total_passed}/{total_possible} passed). Worth a closer look but not a clear winner."
    else:
        result.rating = "red"
        result.headline = f"Weak signal profile ({total_passed}/{total_possible} passed). Several concerns to address."

    return result


# ============================================================
# Category summaries — plain English, NO thresholds exposed
# ============================================================

def _trend_summary(passed: int, total: int, r: ScoreResult) -> str:
    if total == 0: return "Could not assess trend."
    if passed == total:
        return "Strong uptrend — price above key moving averages and near 52-week high."
    if passed >= total * 0.6:
        return "Generally favorable trend with some weak spots."
    if passed >= total * 0.3:
        return "Trend is mixed — some signals positive, others concerning."
    return "Trend is weak — price below key moving averages."


def _momentum_summary(passed: int, total: int, r: ScoreResult) -> str:
    if total == 0 or r.rsi is None:
        return "Could not assess momentum."
    if passed == total:
        return f"Healthy momentum (RSI {r.rsi:.0f})."
    if passed >= total * 0.6:
        return f"Momentum acceptable but watch for shifts (RSI {r.rsi:.0f})."
    if r.rsi >= 70:
        return f"Overbought territory (RSI {r.rsi:.0f}) — risk of pullback."
    if r.rsi <= 35:
        return f"Oversold or weak momentum (RSI {r.rsi:.0f})."
    return f"Momentum signals are mixed (RSI {r.rsi:.0f})."


def _volume_summary(passed: int, total: int, r: ScoreResult) -> str:
    if total == 0 or r.volume_ratio is None:
        return "Could not assess volume."
    if r.volume_ratio >= 1.5:
        return f"Elevated volume ({r.volume_ratio:.1f}x average) — meaningful activity."
    if r.volume_ratio >= 0.8:
        return f"Normal volume ({r.volume_ratio:.1f}x average)."
    return f"Low volume ({r.volume_ratio:.1f}x average) — less conviction in current move."


def _sector_summary(passed: int, total: int, regime: str, r: ScoreResult) -> str:
    if regime == "tailwind":
        return f"Sector ({r.sector}) is outperforming the market — tailwind for stocks in this group."
    if regime == "mild_headwind":
        return f"Sector ({r.sector}) is slightly underperforming the market — mild headwind."
    if regime == "hard_headwind":
        return f"Sector ({r.sector}) is significantly underperforming — strong headwind for stocks in this group."
    return "Sector context unavailable."


def _fundamentals_summary(passed: int, total: int, r: ScoreResult, info: dict) -> str:
    parts = []
    target = info.get("targetMeanPrice")
    if target and r.price and target > r.price:
        upside = (target - r.price) / r.price * 100
        parts.append(f"analyst target ${target:.2f} ({upside:+.1f}% upside)")
    if r.market_cap:
        if r.market_cap >= 200_000_000_000:
            parts.append("mega-cap")
        elif r.market_cap >= 10_000_000_000:
            parts.append("large-cap")
        elif r.market_cap >= 2_000_000_000:
            parts.append("mid-cap")
        else:
            parts.append("small-cap")
    if parts:
        return " · ".join(parts).capitalize()
    return "Limited fundamental data available."


# ============================================================
# Batch scoring entry point
# ============================================================

def score_tickers(tickers: list[str]) -> list[ScoreResult]:
    """Score multiple tickers. Pre-fetches sector data once."""
    sector_data = _get_sector_data()
    results = []
    for t in tickers:
        try:
            results.append(score_ticker(t, sector_data=sector_data))
        except Exception as e:
            log.error(f"Failed to score {t}: {e}")
            results.append(ScoreResult(
                ticker=t.upper(), rating="insufficient_data",
                headline=f"Error scoring {t}: {e}",
                errors=[str(e)],
            ))
    return results

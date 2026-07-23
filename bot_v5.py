"""
Solana Memecoin Sniper Bot v5
Data: DEXScreener (price/volume/liquidity) + Rugcheck.xyz (full security suite)
        + CoinGecko trending (free, no key) for auto-adapting lore

New in v5:
  - Tiny built-in "keep-alive" web server (aiohttp.web) so this can run on
    Render's free tier without being put to sleep. Render's free Web
    Services sleep after 15 minutes with no incoming HTTP traffic — this
    adds a minimal HTTP endpoint you can ping (with the free UptimeRobot
    service) every ~10 minutes to keep it awake. No extra install needed —
    aiohttp already includes this.

⚠️ IMPORTANT — VERIFY BEFORE RELYING ON THIS:
Two fields below (PRE_BOND_DEX_IDS and the holder-count field name in
parse_rugcheck) depend on the exact shape of DEXScreener/Rugcheck's live
API responses, which can change and which I could not test live from here.
Run the bot for a few minutes first, watch the log output, and confirm real
launches are being correctly classified as pre-bond before you treat alerts
as reliable.
No API keys required (besides your Telegram bot token).
"""

import asyncio
import os
import re
import time
import logging
from typing import Optional
import aiohttp
from aiohttp import web
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── CONFIG ──────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
# NOTE: previously had your real token/chat ID hardcoded as fallback
# defaults here — removed since this file is going to GitHub. Set both as
# real environment variables wherever you deploy this (Render, etc.) — the
# startup check below will now fail loudly and immediately if either is
# missing, instead of silently using an old hardcoded value.
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit(
        "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID environment variables "
        "are not set. Set them in your hosting platform's environment "
        "variables before running this bot."
    )

POLL_INTERVAL_SECONDS = 30

# ── Launch filters ────────────────────────────────────────────────────────────
MIN_LIQUIDITY_USD    = 2_000   # lowered from 5,000 — matches the new 7k
                                # market cap floor, avoiding the same
                                # stale-conflict pattern we just fixed above
MAX_LIQUIDITY_USD    = 100_000  # raised way up from 20,000 — that number was
                                # set early on, before MAX_MARKET_CAP_USD got
                                # raised to 32,000 later. Since liquidity
                                # scales with market cap, the old 20k ceiling
                                # was very likely silently rejecting anything
                                # in the upper half of your current mcap
                                # range. Removed as a meaningful gate — the
                                # mcap range + volume/mcap ratio below now do
                                # the real filtering work instead.
MAX_TOKEN_AGE_MINS   = 60      # raised from 15 — the Alpha Playz data shows
                               # age varies 2-31+ mins with no clear pattern,
                               # so market cap (not age) is the real gate
MIN_TOKEN_AGE_MINS   = 3      # synced to your manual tweak
                               # on pure bot/sniper volume before real buyers show up
MIN_VOLUME_5M_USD    = 1_000
MIN_TXNS_5M          = 10

# ── Volume relative to market cap (THE key momentum signal) ──────────────────
# You confirmed market cap isn't fixed (~20k-30k+ observed) — so this ratio
# is doing the real work, not the market cap window above. It scales with
# token size, which a flat dollar figure never could: a 5m volume of $15K
# means something very different on a $20K mcap token vs a $100K one.
MIN_VOL5M_TO_MCAP_RATIO = 0.25  # loosened from 0.5 per your call

# ── Market cap range (NEW) ────────────────────────────────────────────────────
# Keeps alerts in the "still early, room to run" zone instead of catching
# tokens after most of the move already happened.
MIN_MARKET_CAP_USD = 7_000    # lowered from 15k per your call — more room
MAX_MARKET_CAP_USD = 32_000   # per your call
                               # market cap isn't the precise trigger, 5m
                               # volume relative to mcap is (see below).
                               # This window is now a loose early-stage
                               # boundary, not the main filter doing the work.

# ── Dip alerts (toggle) ───────────────────────────────────────────────────────
ENABLE_DIP_ALERTS = False   # OFF per your manual tweak — dip re-entry alone
                            # stays on and does the real work now

# ── Reject already-pumped tokens (NEW) ────────────────────────────────────────
# If a token is already up huge in the last hour by the time the bot sees
# it, you're late — this filters those out rather than alerting on the tail
# end of a move that already happened.
MAX_PRICE_CHANGE_H1_PCT = 500   # raised from 300 — a launch-to-20k move can
                                 # easily be ~500-600%, don't reject those

# ── Pre-bond only filter (NEW) ────────────────────────────────────────────────
# You said you only want plays that haven't bonded (still on the pump.fun
# bonding curve, not yet migrated to Raydium). DEXScreener tags pairs with a
# "dexId" field — bonding-curve pairs use a different dexId than post-migration
# Raydium pairs. The values below are the commonly reported ones, but you
# MUST confirm this yourself once the bot is running (see note at top of file).
#
# How to confirm: watch your terminal log — every rejected token now logs its
# dexId (see "Pre-bond fail" log lines). Pick a token you know is still
# bonding on pump.fun (check it on pump.fun directly), find its address, and
# see what dexId your bot logs for it. Add that exact string to the set below
# if it isn't already there.
ONLY_PRE_BOND     = True
PRE_BOND_DEX_IDS  = {"pumpfun", "pump.fun", "pump-fun", "pumpfun-launchpad", "pump-fun-launchpad"}
# Reverted per your latest call — pumpswap and raydium removed again since
# both are POST-bond markets (a token only trades there after migrating).
# This is now strict pre-bond-only: only pure pump.fun bonding-curve pairs
# will pass. If you want bonded plays back in later, just add "pumpswap"
# and/or "raydium" back to the set above.

# ── Broader Rugcheck risk-level check (NEW) ──────────────────────────────────
# The old version only checked mint/freeze/bundle individually. Rugcheck
# actually returns a full list of risk flags with severity levels — this
# catches everything else (e.g. "high mint concentration", "low liquidity
# lock", copycat metadata, etc.) without you having to name each one.
REJECT_DANGER_RISKS = True
DANGER_RISK_LEVELS  = {"danger", "high"}

# ── Minimum holder count (NEW) ────────────────────────────────────────────────
# Guards against tokens with almost no real distribution yet.
# NOTE: confirm "totalHolders" is the right field — see parse_rugcheck() below.
MIN_HOLDERS = 20   # dialed back from 50 — 7k mcap tokens are much earlier
                    # stage, haven't had time to accumulate 50+ holders yet

# ── Rugcheck.xyz security filters ────────────────────────────────────────────
MAX_RUGCHECK_SCORE      = 70   # raised from 50 per your call — more lenient
                                # given the scale is still not fully verified
# NOTE: changed from 500. Now filtering on Rugcheck's "score_normalised"
# field (0-100 scale, higher = riskier), not the old unbounded raw score.
# 50 is a reasonable starting midpoint but I can't verify Rugcheck's exact
# scale meaning from here — watch your log for a stretch and see whether
# real, obviously-safe tokens are getting rejected on this specific reason;
# if so, raise this number. If obviously risky ones are passing, lower it.
REQUIRE_MINT_REVOKED    = True
REQUIRE_FREEZE_DISABLED = True
REQUIRE_LP_BURNED       = True   # turned ON per your call
MAX_DEV_WALLET_PCT      = 5.0    # loosened from 2.0 — still tight, less brittle
MAX_TOP10_HOLDER_PCT    = 35.0   # loosened from 30 per your call
REQUIRE_TOP10_CHECK     = False  # OFF per your call — top10% no longer
                                  # blocks a token, just shown for info in
                                  # every alert. Flip to True to re-enable
                                  # as a hard filter, using the cap above.
MAX_SNIPER_COUNT        = 3   # synced to your manual tweak
REJECT_BUNDLED          = True
# NOTE: this is a REJECT filter, not just a notice — a token flagged as
# bundled never reaches your Telegram channel at all; it's only visible in
# the terminal log as a rejection reason. If a real percentage is available
# (see bundled_pct below), rejection uses that threshold instead of a flat
# yes/no, so mildly-bundled tokens with a real story can still pass.
MAX_BUNDLE_PCT          = 30.0   # synced to your manual tweak

# ── Buy/sell health ───────────────────────────────────────────────────────────
MIN_BUY_SELL_RATIO = 0.2   # loosened from 0.3

# ── Lore / social filters ─────────────────────────────────────────────────────
REQUIRE_AT_LEAST_ONE_SOCIAL = True   # Turned back ON — you've observed
                                     # plays with zero X post/description
                                     # correlate strongly with rugs. A token
                                     # now needs either a linked social OR a
                                     # real written description to pass.
PREFER_TWITTER              = True   # X/Twitter gets a lore bonus in the summary
MIN_LORE_SCORE              = 1      # 0 = no socials at all → reject
                                     # 1 = at least one social → pass
                                     # 2 = two or more socials → stricter

# ── Lore filter leniency (NEW) ────────────────────────────────────────────────
# A token can pass the lore check EITHER by having a linked social account
# OR by having a real written description (its on-chain "about" blurb).
# This stops rejecting tokens purely for lacking a dedicated X/Telegram page
# when they actually have a clear, specific story attached to them.
MIN_DESCRIPTION_LENGTH = 40   # characters — filters out empty/junk descriptions

# ── Trending lore matching (NEW) ──────────────────────────────────────────────
# Two sources feed the trending keyword list:
#  1. MANUAL: trending_lore.txt — you edit this yourself, any time, no restart
#     needed. One keyword per line. e.g. "ansem", "robinhood". The bot
#     re-reads this file every single loop, so updates apply within 30s.
#  2. AUTO: CoinGecko's free trending-search endpoint (no key needed),
#     refreshed periodically. This is a real but imperfect proxy for "what's
#     hot right now" — it skews toward bigger/established trending coins
#     rather than brand-new pump.fun narratives, but it's genuinely free and
#     genuinely live, so it adds some auto-adapting on top of your own list.
#     There is no free way to pull live X/Twitter trending topics — that
#     requires paid API access — so this is the realistic alternative.
TRENDING_LORE_FILE          = "trending_lore.txt"
USE_COINGECKO_AUTO_TRENDING = True
COINGECKO_REFRESH_SECONDS   = 600   # refresh auto list every 10 min

# NEW: news-driven trending (cryptocurrency.cv) — free, no API key, pulls
# real trending topics from 130+ crypto news outlets. Complements CoinGecko's
# price-based trending with narrative/news-driven signal.
USE_CRYPTOCV_TRENDING       = True
CRYPTOCV_REFRESH_SECONDS    = 600   # refresh every 10 min
TRENDING_LORE_ONLY          = False  # if True: ONLY alert tokens matching a
                                      # trending keyword (aggressive, narrow)

# ── Dip detection ─────────────────────────────────────────────────────────────
PUMP_MIN_PCT     = 50
DIP_THRESHOLDS   = [
    {"label": "🟡 Mild Dip",   "pct": 20},
    {"label": "🟠 Medium Dip", "pct": 30},
    {"label": "🔴 Hard Dip",   "pct": 40},
]
VOLUME_SPIKE_RATIO = 4.0   # raised from 2.5 — now only used as confirmation
                           # inside dip/re-entry alerts (no standalone volume
                           # alert anymore), so requiring a bigger spike makes
                           # sense: needs 5m volume at 4x the recent average

# ── Dip re-entry alerts (NEW) ──────────────────────────────────────────────────
# Catches the "pumped once, dipped, volume flowing back in" moment instead
# of just the initial launch pump. The idea: the first pump is often
# sniper/bot-driven and dumps right after — the more meaningful move (if
# there is one) tends to come once real buyers step back in after the dip.
#
# Dip depth varies wildly per coin (some meaningful bounces start from a
# -5% pullback, others from -30%+) so there's no fixed drop-percentage
# window anymore — ANY dip below peak counts. What actually gates the
# alert is the bounce + volume confirmation, and that bar itself flexes
# based on how strong the token's lore looked when it first got alerted:
# a trending-keyword match clears the bar more easily, a token with no
# lore signal at all has to prove itself harder with a bigger bounce.
# ── Liquidity-collapse guard (NEW) ────────────────────────────────────────────
# A real rug pulls liquidity, not just price. But once liquidity is nearly
# gone, the tiny remaining pool can still produce wild, erratic price swings
# from small trades — which can look like a "dip + bounce + volume" reentry
# pattern even on a token that's already dead. This tracks each token's
# liquidity against its own starting point, and once it's collapsed past
# the threshold below, treats the token as presumed rugged and silently
# stops sending ANY further alerts about it (dip, reentry, multiplier) —
# no more "reversal!" messages on something that's already a corpse.
LIQUIDITY_RUG_DROP_PCT = 75   # presume rugged if liquidity drops this % or
                              # more from where it was when first tracked

ENABLE_DIP_REENTRY_ALERTS      = True
DIP_REENTRY_MIN_BOUNCE_STRONG_LORE = 5    # % bounce needed if it had a
                                           # trending-keyword match at launch
DIP_REENTRY_MIN_BOUNCE_WEAK_LORE   = 12   # % bounce needed if it didn't

# ── Multiplier alerts (NEW) ───────────────────────────────────────────────────
# Fires once per milestone as a token's price/market cap climbs from where
# the bot first alerted it. Each threshold only fires once per token.
MULTIPLIER_THRESHOLDS = [2, 3, 5, 10, 20, 50, 100, 250, 500, 1000]
# If a token somehow keeps running past 1000x (rare, but memecoins do wild
# things), the bot auto-continues alerting every extra 1000x step forever
# (2000x, 3000x, ...) — you don't need to keep manually extending this list.

# ── GMGN integration (NEW) ────────────────────────────────────────────────────
# Runs alongside your existing DEXScreener+Rugcheck pipeline as a SECOND,
# independent discovery source — doesn't replace anything, just adds another
# way to catch launches, tagged separately in alerts so you can compare.
# Requires: Node.js + `npm install -g gmgn-cli` + GMGN_API_KEY env var set
# (all already done in your setup). Confirmed field names/scale from your
# real gmgn_sample.json — "progress" is a clean 0-1 fraction (0.1469 = 14.69%
# bonded), "_rate"/"_ratio" fields are all 0-1 fractions too.
ENABLE_GMGN_SOURCE      = True
GMGN_CLI_TIMEOUT_SEC    = 45     # raised from 20 — a full snapshot pull may
                                  # genuinely take a while; raise further if
                                  # you're still seeing timeouts after this
GMGN_MAX_PROGRESS       = 0.99   # must be under this to count as pre-bond

# ── New GMGN signal-based filters (NEW) ────────────────────────────────────
MAX_INSIDER_HOLD_PCT    = 20.0   # reject if suspected insiders hold more
                                  # than this % of supply
REJECT_DUPLICATE_ASSETS = True   # reject if image/X/Telegram/website was
                                  # reused from another token launch

# ── Creator history filter (NEW) ───────────────────────────────────────────
# GMGN gives us creator_created_count and creator_created_open_ratio —
# essentially the same "how many tokens has this dev launched, and what %
# actually bonded" check you do manually. A creator with 1,000 launches and
# under 100 bonded is a red flag no matter how good the lore looks; a
# creator with 12 launches and 5 bonded is decent, especially with good lore.
#
# Leniency for new creators: below CREATOR_HISTORY_MIN_SAMPLE prior
# launches, the ratio is statistically meaningless (a creator's 1st or 2nd
# token can only ever show 0% or 100% bonded — neither means anything yet),
# so the ratio check is skipped entirely and lore alone decides.
CREATOR_HISTORY_MIN_SAMPLE       = 3     # need at least this many prior
                                          # launches before judging by ratio
CREATOR_MIN_BOND_RATIO_FLOOR     = 0.10  # below this = hard reject, lore
                                          # can't save it (only applies once
                                          # sample size is met)
CREATOR_MIN_BOND_RATIO_LENIENT   = 0.30  # between floor and this = only
                                          # passes with good lore; at or
                                          # above this = fine regardless
                                  # (progress is 0-1; 0.99 = under 99% bonded)
# NOTE: GMGN's own "liquidity" field appears to be denominated in SOL (its
# quote_address matches SOL's mint), not USD — I didn't build a filter on
# it since converting SOL->USD reliably needs a live price feed I'd be
# guessing at. Market cap does the equivalent "is this too small/big" job.

# ── State ─────────────────────────────────────────────────────────────────────
tracked_tokens: dict = {}
_last_hourly_report: float = 0.0

# NEW: persistent, all-time records — these never get cleared or pruned
# (unlike tracked_tokens, which drops tokens after 6 hours). This is what
# actually fixes the hourly report only showing a live snapshot: these two
# accumulate for as long as the bot has been running, so a token that
# spiked to 3x and came back down before the report ran still shows up.
_total_tokens_caught: int = 0        # every launch alert ever sent this run
_all_time_2x_log: list    = []       # every 2x+ milestone ever hit this run

# ── Hourly performance report (NEW) ───────────────────────────────────────────
HOURLY_REPORT_INTERVAL_SEC  = 3600  # send every 60 minutes
HOURLY_REPORT_MIN_GAIN_PCT  = 20    # only list tokens up at least this % from
                                     # their launch-alert price — "done well,"
                                     # not just "still exists"
HOURLY_REPORT_MAX_LISTED    = 15    # cap the list so it doesn't get unwieldy

# ── 24-hour top performers report (NEW) ───────────────────────────────────────
# Built entirely from _all_time_2x_log below (already timestamped), so this
# doesn't depend on tracked_tokens at all — meaning it's unaffected by the
# 6-hour tracked-tokens cleanup elsewhere in the loop. Purely additive: only
# reads _all_time_2x_log, never modifies it, so the existing hourly report's
# own "all-time since bot started" section keeps working exactly as before.
DAILY_REPORT_INTERVAL_SEC = 24 * 3600   # send every 24 hours
DAILY_REPORT_MAX_LISTED   = 15          # cap the list, same idea as hourly
_last_24h_report: float = 0.0
_trending_cache: dict = {
    "coingecko": set(), "coingecko_fetched_at": 0.0,
    "cryptocv": set(), "cryptocv_fetched_at": 0.0,
}

# ─── DEXSCREENER ─────────────────────────────────────────────────────────────

DEXSCREENER_BASE = "https://api.dexscreener.com"

async def fetch_new_solana_profiles(session: aiohttp.ClientSession) -> list:
    url = f"{DEXSCREENER_BASE}/token-profiles/latest/v1"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return []
            data = await r.json()
            return [t for t in data if t.get("chainId") == "solana"]
    except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as e:
        log.error(f"DEXScreener profiles error: {e}")
        return []

async def fetch_token_pairs(session: aiohttp.ClientSession, token_address: str) -> list:
    url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return []
            data = await r.json()
            return [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
    except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as e:
        log.error(f"DEXScreener pairs error: {e}")
        return []

# ─── RUGCHECK.XYZ ────────────────────────────────────────────────────────────

RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"

async def fetch_rugcheck(session: aiohttp.ClientSession, token_address: str) -> Optional[dict]:
    # NOTE: switched from /report/summary to the full /report endpoint.
    # The summary endpoint appears to omit detailed top-holder / bundle data
    # (which is very likely why top10% was showing as "?" in your alerts).
    # The full report is a bigger response but has the detail you asked for.
    url = f"{RUGCHECK_BASE}/tokens/{token_address}/report"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as e:
        log.error(f"Rugcheck error: {e}")
        return None

def parse_rugcheck(report: dict) -> dict:
    risks      = report.get("risks", [])
    risk_names = [r.get("name", "").lower() for r in risks]

    top_holders = report.get("topHolders", [])
    top10_pct   = sum(h.get("pct", 0) for h in top_holders[:10]) if top_holders else None
    # NOTE: fixed a bug here — the full /report endpoint returns each
    # holder's "pct" already as a percentage number (e.g. 4.2 meaning
    # 4.2%), not a 0-1 fraction. The old code multiplied by 100 on top of
    # that, producing impossible values like "4242.1%". Removed that.
    dev_pct     = top_holders[0].get("pct", 0) if top_holders else None
    # NOTE: fixed the same double-multiplication bug here as top10_pct above
    # — "pct" is already a plain percentage number, the old *100 was wrong.
    # Also worth flagging: this assumes the single largest holder wallet is
    # the dev — Rugcheck's full report doesn't explicitly tag "creator" on
    # this field as far as I can tell from here, so this is a reasonable
    # proxy, not a guaranteed-accurate one.

    markets   = report.get("markets", [])
    lp_burned = False
    lp_locked = False
    if markets:
        lp        = markets[0].get("lp", {})
        lp_burned = lp.get("lpBurned", False)
        lp_locked = lp.get("lpLocked", 0) > 0

    is_bundled   = any("bundle" in n for n in risk_names)

    # NEW: try to pull an actual bundle percentage instead of just yes/no.
    # Rugcheck risk entries often carry a "value" or "description" string
    # containing a percentage (e.g. "23.4%") — extract it if present.
    bundled_pct = None
    for r in risks:
        name = (r.get("name") or "").lower()
        if "bundle" in name:
            for field in (r.get("value"), r.get("description")):
                if not field:
                    continue
                match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(field))
                if match:
                    bundled_pct = float(match.group(1))
                    break
            if bundled_pct is not None:
                break

    sniper_count = report.get("sniperCount", 0) or 0

    token_meta     = report.get("token", {})
    mint_revoked   = not token_meta.get("mintAuthority")
    freeze_revoked = not token_meta.get("freezeAuthority")
    is_mutable_meta = any("mutable" in n for n in risk_names)

    # NOTE: fixed a bug here — the full /report endpoint's raw "score"
    # field is an unbounded number (can be in the hundreds of thousands),
    # not the smaller scale the summary endpoint used. Rugcheck's full
    # report also includes "score_normalised" (0-100 scale), which is what
    # MAX_RUGCHECK_SCORE below is actually calibrated for. Falls back to
    # the raw score only if score_normalised isn't present.
    score = report.get("score_normalised")
    if score is None:
        score = report.get("score", 9999)

    # NEW: holder count. Rugcheck's summary endpoint field name for this can
    # vary — trying the most common ones. If MIN_HOLDERS filtering seems to
    # always pass/fail incorrectly, print(report.keys()) once to find the
    # right field and adjust the line below.
    holders = (
        report.get("totalHolders")
        or report.get("holderCount")
        or report.get("holders")
    )

    # ── Lore / social data from Rugcheck metadata ─────────────────────────────
    # Rugcheck returns token metadata which often includes socials
    meta       = report.get("tokenMeta", {}) or {}
    extensions = meta.get("extensions", {}) or {}

    # Also check top-level fields Rugcheck sometimes puts socials in
    raw_twitter  = (
        extensions.get("twitter")
        or extensions.get("x")
        or report.get("twitter")
        or ""
    )
    raw_telegram = (
        extensions.get("telegram")
        or report.get("telegram")
        or ""
    )
    raw_website  = (
        extensions.get("website")
        or report.get("website")
        or ""
    )
    raw_discord  = (
        extensions.get("discord")
        or report.get("discord")
        or ""
    )
    description = (
        extensions.get("description")
        or meta.get("description")
        or report.get("description")
        or ""
    )

    def clean_url(u: str) -> str:
        u = u.strip()
        if u and not u.startswith("http"):
            u = "https://" + u
        return u

    twitter  = clean_url(raw_twitter)
    telegram = clean_url(raw_telegram)
    website  = clean_url(raw_website)
    discord  = clean_url(raw_discord)

    # Lore score: count available socials
    socials_found = [s for s in [twitter, telegram, website, discord] if s]
    lore_score    = len(socials_found)
    has_twitter   = bool(twitter)

    return {
        "score":          score,
        "holders":        holders,
        "mint_revoked":   mint_revoked,
        "freeze_revoked": freeze_revoked,
        "lp_burned":      lp_burned,
        "lp_locked":      lp_locked,
        "top10_pct":      top10_pct,
        "dev_pct":        dev_pct,
        "is_bundled":     is_bundled,
        "bundled_pct":    bundled_pct,
        "sniper_count":   sniper_count,
        "mutable_meta":   is_mutable_meta,
        "risks":          risks,
        # Lore
        "twitter":        twitter,
        "telegram":       telegram,
        "website":        website,
        "discord":        discord,
        "description":    description,
        "lore_score":     lore_score,
        "has_twitter":    has_twitter,
        "socials_found":  socials_found,
    }

# ─── GMGN (NEW) ──────────────────────────────────────────────────────────────

async def fetch_gmgn_trenches(token_type: str = "new_creation") -> list:
    """Runs the gmgn-cli tool as a subprocess and returns the list of tokens
    from the requested category ("new_creation", "almost_bonded", etc).
    One-shot call — confirmed it returns a full snapshot and exits, not a
    continuous stream, so this fits the same 30s poll pattern as everything
    else. Requires gmgn-cli installed globally and GMGN_API_KEY set.

    NOTE: uses create_subprocess_shell (not create_subprocess_exec). On
    Windows, npm installs global CLI tools as a "gmgn-cli.cmd" wrapper
    script, not a plain "gmgn-cli" executable — PowerShell resolves that
    automatically when you type the command, but Python's exec-style
    subprocess call does NOT do that lookup and would fail to find it.
    Going through the shell matches what already works in your terminal."""
    import json as _json
    cmd = (
        'gmgn-cli market trenches '
        '--chain sol '
        f'--type "{token_type}" '
        '--launchpad-platform "Pump.fun" '
        '--raw'
    )
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=GMGN_CLI_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            proc.kill()
            log.error("gmgn-cli timed out, killed subprocess")
            return []

        if proc.returncode != 0:
            log.error(f"gmgn-cli exited with error: {stderr.decode(errors='replace')[:300]}")
            return []

        data = _json.loads(stdout.decode("utf-8-sig", errors="replace"))
        return data.get(token_type, []) or []
    except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as e:
        log.error(f"GMGN fetch error: {e}")
        return []


def passes_gmgn_filters(token: dict) -> tuple[bool, str]:
    """Maps GMGN's own fields onto the same thresholds you've already tuned
    for the DEXScreener/Rugcheck pipeline, so both sources hold tokens to
    a consistent bar."""
    mcap = token.get("usd_market_cap") or token.get("market_cap") or 0
    if mcap < MIN_MARKET_CAP_USD:
        return False, f"Market cap too low (${mcap:,.0f})"
    if mcap > MAX_MARKET_CAP_USD:
        return False, f"Market cap too high (${mcap:,.0f})"

    if ONLY_PRE_BOND:
        progress = token.get("progress", 0) or 0
        if progress >= GMGN_MAX_PROGRESS:
            return False, f"Already bonded (progress={progress:.1%})"

    if REQUIRE_MINT_REVOKED and not token.get("renounced_mint"):
        return False, "Mint authority NOT revoked"
    if REQUIRE_FREEZE_DISABLED and not token.get("renounced_freeze_account"):
        return False, "Freeze authority NOT disabled"

    holders = token.get("holder_count")
    if holders is not None and holders < MIN_HOLDERS:
        return False, f"Too few holders ({holders})"

    if REQUIRE_TOP10_CHECK:
        top10_pct = (token.get("top_10_holder_rate") or 0) * 100
        if top10_pct > MAX_TOP10_HOLDER_PCT:
            return False, f"Top 10 holders = {top10_pct:.1f}%"

    dev_pct = (token.get("dev_team_hold_rate") or 0) * 100
    if dev_pct > MAX_DEV_WALLET_PCT:
        return False, f"Dev holds {dev_pct:.1f}%"

    sniper_count = token.get("sniper_count", 0) or 0
    if sniper_count > MAX_SNIPER_COUNT:
        return False, f"Too many snipers ({sniper_count})"

    honeypot = str(token.get("is_honeypot", "") or "").lower()
    if honeypot in ("true", "1", "yes"):
        return False, "Flagged as honeypot"

    # NEW: wash trading flag — GMGN calculates this directly, we just
    # weren't checking it
    if token.get("is_wash_trading"):
        return False, "Flagged as wash trading"

    # NEW: suspected insider holding — separate metric from dev wallet %
    # and top-10 holder %, worth its own check
    insider_pct = (token.get("suspected_insider_hold_rate") or 0) * 100
    if insider_pct > MAX_INSIDER_HOLD_PCT:
        return False, f"Suspected insiders hold {insider_pct:.1f}%"

    # NEW: duplicate asset check — image/X/Telegram/website reused from
    # another token launch is one of the most classic serial-scammer
    # fingerprints there is
    if REJECT_DUPLICATE_ASSETS:
        dup_fields = {
            "image":    token.get("image_dup", 0) or 0,
            "twitter":  token.get("twitter_dup", 0) or 0,
            "telegram": token.get("telegram_dup", 0) or 0,
            "website":  token.get("website_dup", 0) or 0,
        }
        dup_hits = [name for name, count in dup_fields.items() if count > 0]
        if dup_hits:
            return False, f"Reused asset(s) from other launches: {', '.join(dup_hits)}"

    # NOTE: CEX-funded creator wallet ("fund_from") is surfaced in the
    # alert message rather than filtered here — plenty of legitimate
    # creators fund from an exchange too, so this is informational (like
    # top10%) rather than a hard gate. See format_gmgn_launch_alert.

    # NEW: LP burn check — this existed on the DEXScreener/Rugcheck path
    # but was completely missing here. Since REQUIRE_LP_BURNED got turned
    # on, GMGN-discovered tokens were passing through with this
    # requirement silently unenforced — a real gap, likely part of why
    # more rugs were slipping through recently.
    if REQUIRE_LP_BURNED:
        burn_status = str(token.get("burn_status", "") or "").lower()
        if burn_status != "burn":
            return False, f"LP not burned (status: '{burn_status}')"

    # NEW: creator history check — this is the "how many tokens has this
    # dev launched, and what % actually bonded" filter.
    creator_count = token.get("creator_created_count", 0) or 0
    creator_ratio = token.get("creator_created_open_ratio")

    if creator_count >= CREATOR_HISTORY_MIN_SAMPLE and creator_ratio is not None:
        if creator_ratio < CREATOR_MIN_BOND_RATIO_FLOOR:
            return False, (
                f"Creator history too bad: {creator_ratio:.0%} bond rate "
                f"across {creator_count} launches — hard floor, lore can't save it"
            )
        if creator_ratio < CREATOR_MIN_BOND_RATIO_LENIENT:
            # Middle zone — only passes with real lore behind it
            has_lore = bool(
                token.get("has_at_least_one_social")
                or token.get("twitter")
                or token.get("twitter_handle")
                or token.get("telegram")
                or token.get("website")
            )
            if not has_lore:
                return False, (
                    f"Creator history mediocre ({creator_ratio:.0%} bond rate, "
                    f"{creator_count} launches) and no lore to offset it"
                )
    # else: creator has fewer than CREATOR_HISTORY_MIN_SAMPLE prior launches
    # — not enough history to judge by ratio yet, so no penalty here. Lore
    # requirement (checked separately below) still applies as normal.

    # NEW: lore requirement — this check existed on the DEXScreener path
    # but was missing here entirely, meaning GMGN-discovered tokens could
    # pass with zero social presence even while REQUIRE_AT_LEAST_ONE_SOCIAL
    # was on. Using GMGN's own social fields as the equivalent check.
    if REQUIRE_AT_LEAST_ONE_SOCIAL:
        has_social = bool(
            token.get("has_at_least_one_social")
            or token.get("twitter")
            or token.get("twitter_handle")
            or token.get("telegram")
            or token.get("website")
        )
        if not has_social:
            return False, "No socials found — no lore, no send"

    return True, ""


def format_gmgn_launch_alert(token: dict, meta_info: dict = None) -> str:
    addr    = token.get("address", "")
    dex_url = f"https://dexscreener.com/solana/{addr}"
    birdeye = f"https://birdeye.so/token/{addr}?chain=solana"
    ruglink = f"https://rugcheck.xyz/tokens/{addr}"

    mcap        = token.get("usd_market_cap") or token.get("market_cap") or 0
    progress    = (token.get("progress", 0) or 0) * 100
    holders     = token.get("holder_count", "?")
    top10_pct   = (token.get("top_10_holder_rate") or 0) * 100
    dev_pct     = (token.get("dev_team_hold_rate") or 0) * 100
    insider_pct = (token.get("suspected_insider_hold_rate") or 0) * 100
    sniper      = token.get("sniper_count", 0) or 0
    vol24h      = token.get("volume_24h", 0) or 0
    fund_from   = token.get("fund_from") or token.get("fund_from_address") or ""

    twitter  = token.get("twitter") or token.get("twitter_handle") or ""
    telegram = token.get("telegram") or ""
    website  = token.get("website") or ""

    lore_lines = []
    lore_lines.append(f"  🐦 X/Twitter: {'✅ ' + twitter if twitter else '❌ none'}")
    if telegram:
        lore_lines.append(f"  ✈️ Telegram: {telegram}")
    if website:
        lore_lines.append(f"  🌐 Website: {website}")

    fund_line = f"💰 Creator funded from: `{fund_from}`\n" if fund_from else ""

    narrative_meta_line = ""
    if meta_info and meta_info.get("categories"):
        cats = meta_info["categories"]
        heat = meta_info.get("heat", {})
        parts = []
        for c in cats:
            h = heat.get(c, 0)
            heat_tag = f" (🔥 {h} similar caught recently — heating up)" if h >= META_HEAT_MIN_COUNT else ""
            parts.append(f"{c}{heat_tag}")
        narrative_meta_line = f"🎭 *Meta:* {', '.join(parts)}\n\n"

    return (
        f"🟣 *GMGN NEW LAUNCH* 🟣\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{narrative_meta_line}"
        f"*{token.get('name','?')}* `{token.get('symbol','?')}`\n"
        f"📋 `{addr}`\n\n"
        f"📊 Market Cap: `${mcap:,.0f}`\n"
        f"🎯 Bonding Progress: `{progress:.1f}%`\n"
        f"👥 Holders: `{holders}`\n"
        f"🔝 Top 10 Holders: `{top10_pct:.1f}%`\n"
        f"👤 Dev Wallet: `{dev_pct:.1f}%`\n"
        f"🕵️ Suspected Insiders: `{insider_pct:.1f}%`\n"
        f"🎯 Snipers: `{sniper}`\n"
        f"{fund_line}"
        f"📈 24h Volume: `${vol24h:,.0f}`\n\n"
        f"🎭 *Lore*\n" + "\n".join(lore_lines) + "\n\n"
        f"🔗 [DEXScreener]({dex_url}) | [Birdeye]({birdeye}) | [Rugcheck]({ruglink})"
    )


async def process_new_gmgn_token(bot: Bot, token: dict) -> None:
    """Same idea as process_new_token, but for a token that came from
    GMGN's already-rich single-call data instead of DEXScreener+Rugcheck."""
    token_address = token.get("address")
    if not token_address or token_address in tracked_tokens:
        return

    ok, reason = passes_gmgn_filters(token)
    if not ok:
        log.info(f"GMGN fail [{token_address[:8]}]: {reason}")
        return

    # NEW: meta classification, same idea as the DEXScreener path
    meta_text_blob = " ".join([
        token.get("name", "") or "",
        token.get("symbol", "") or "",
    ])
    meta_categories_dict = load_meta_categories()
    matched_categories    = classify_token_meta(meta_text_blob, meta_categories_dict)
    meta_heat             = get_meta_heat(matched_categories)
    meta_info = {"categories": matched_categories, "heat": meta_heat}
    if matched_categories:
        record_meta_alert(matched_categories)

    price_now = 0.0
    total_supply = token.get("total_supply") or 0
    mcap = token.get("usd_market_cap") or token.get("market_cap") or 0
    if total_supply:
        price_now = mcap / total_supply

    tracked_tokens[token_address] = {
        "first_seen":          time.time(),
        "initial_price":       price_now,
        "price_high":          price_now,
        "price_low_since_peak": None,
        "alerted_reentry":     False,
        "initial_liquidity":   None,   # NEW: captured on first DEXScreener
                                        # fetch during the scan loop, since
                                        # GMGN's own liquidity field is in
                                        # SOL, not USD
        "presumed_rugged":     False,  # NEW
        # GMGN path doesn't run the trending-keyword check the DEXScreener
        # path does — using its own social-presence flag as the closest
        # available proxy for "some lore signal present."
        "had_trending_lore":   bool(token.get("has_at_least_one_social")),
        "initial_mcap":        mcap,
        "alerted_dips":        set(),
        "alerted_multiples":   set(),
        "pair_address":        token.get("pool_address"),
    }

    sym = token.get("symbol", "?")
    log.info(f"🟣 GMGN ALERT: {sym} ({token_address[:8]}...) | mcap=${mcap:,.0f}")

    global _total_tokens_caught
    _total_tokens_caught += 1

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=format_gmgn_launch_alert(token, meta_info),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )

# ─── TRENDING LORE (NEW) ─────────────────────────────────────────────────────

COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"
CRYPTOCV_TRENDING_URL  = "https://cryptocurrency.cv/api/trending"

def load_manual_trending_keywords() -> set:
    """Reads trending_lore.txt fresh every call — edit the file anytime,
    no restart needed. One keyword per line. Lines starting with # are
    ignored (use them for your own notes/dates)."""
    try:
        if not os.path.exists(TRENDING_LORE_FILE):
            return set()
        with open(TRENDING_LORE_FILE, "r", encoding="utf-8") as f:
            return {
                line.strip().lower()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            }
    except Exception as e:
        log.error(f"Trending lore file read error: {e}")
        return set()


async def fetch_coingecko_trending(session: aiohttp.ClientSession) -> set:
    """Free, no API key. Returns lowercase names/symbols currently trending
    on CoinGecko. Skews toward bigger/established coins, not brand-new
    pump.fun tokens — treat as a supplementary signal, not gospel."""
    try:
        async with session.get(COINGECKO_TRENDING_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return set()
            data = await r.json()
            terms = set()
            for c in data.get("coins", []):
                item = c.get("item", {})
                if item.get("name"):
                    terms.add(item["name"].strip().lower())
                if item.get("symbol"):
                    terms.add(item["symbol"].strip().lower())
            return terms
    except Exception as e:
        log.error(f"CoinGecko trending error: {e}")
        return set()


async def fetch_cryptocv_trending(session: aiohttp.ClientSession) -> set:
    """Free, no API key (cryptocurrency.cv) — pulls trending keywords/topics
    sourced from 130+ real news outlets (CoinDesk, The Block, Decrypt,
    CoinTelegraph, etc). This is the "news outlets" signal — catches
    narrative-driven trends (a person, meme, or story suddenly making
    headlines) that a pure price-trending source like CoinGecko might miss.
    NOTE: I parsed this defensively since I don't have full confirmed docs
    on the exact response shape from here — if this consistently returns
    nothing, check https://cryptocurrency.cv/api/trending in a browser to
    see the real JSON shape and let me know so the parsing can be adjusted."""
    try:
        async with session.get(CRYPTOCV_TRENDING_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return set()
            data = await r.json()
            terms = set()
            items = (
                data.get("trending")
                or data.get("topics")
                or data.get("keywords")
                or data.get("data")
                or []
            )
            if isinstance(items, dict):
                items = items.get("topics") or items.get("keywords") or []
            for item in items:
                if isinstance(item, str):
                    terms.add(item.strip().lower())
                elif isinstance(item, dict):
                    val = (
                        item.get("keyword")
                        or item.get("topic")
                        or item.get("name")
                        or item.get("term")
                    )
                    if val:
                        terms.add(str(val).strip().lower())
            return terms
    except Exception as e:
        log.error(f"cryptocurrency.cv trending error: {e}")
        return set()


async def get_trending_keywords(session: aiohttp.ClientSession) -> tuple[set, set]:
    """Returns (manual_keywords, auto_keywords). Auto list merges CoinGecko
    (price-trending) and cryptocurrency.cv (news-trending) sources, each
    refreshed on its own timer so we don't hammer either API every 30s."""
    manual = load_manual_trending_keywords()
    now = time.time()

    if USE_COINGECKO_AUTO_TRENDING:
        if now - _trending_cache["coingecko_fetched_at"] > COINGECKO_REFRESH_SECONDS:
            auto_cg = await fetch_coingecko_trending(session)
            _trending_cache["coingecko"] = auto_cg
            _trending_cache["coingecko_fetched_at"] = now
            if auto_cg:
                log.info(f"🔄 CoinGecko trending refreshed: {sorted(auto_cg)[:12]}")

    if USE_CRYPTOCV_TRENDING:
        if now - _trending_cache["cryptocv_fetched_at"] > CRYPTOCV_REFRESH_SECONDS:
            auto_cv = await fetch_cryptocv_trending(session)
            _trending_cache["cryptocv"] = auto_cv
            _trending_cache["cryptocv_fetched_at"] = now
            if auto_cv:
                log.info(f"🔄 News trending refreshed (cryptocurrency.cv): {sorted(auto_cv)[:12]}")

    combined_auto = _trending_cache["coingecko"] | _trending_cache["cryptocv"]
    return manual, combined_auto


def check_trending_lore(pair: dict, sec: dict, manual_kw: set, auto_kw: set) -> dict:
    """Checks token name/symbol/description/X handle against both keyword
    sets. Substring match, case-insensitive — deliberately loose so
    'robinhood' matches 'ROBINHOODCOIN' etc.
    NEW: keywords under 3 characters are ignored — auto-trending sources
    sometimes surface very short/generic terms (e.g. "cc", "btc", "arb")
    that would otherwise match almost any token name as a false positive."""
    base = pair.get("baseToken", {})
    haystack = " ".join([
        base.get("name", "") or "",
        base.get("symbol", "") or "",
        sec.get("description", "") or "",
        sec.get("twitter", "") or "",
    ]).lower()

    manual_hits = {kw for kw in manual_kw if kw and len(kw) >= 3 and kw in haystack}
    auto_hits   = {kw for kw in auto_kw if kw and len(kw) >= 3 and kw in haystack}

    return {
        "manual_hits": manual_hits,
        "auto_hits":   auto_hits,
        "is_trending": bool(manual_hits or auto_hits),
    }

# ─── META CLASSIFIER (NEW) ───────────────────────────────────────────────────
# Goes a level beyond single-keyword trending matches: classifies each token
# into broader narrative categories (AI-agent coins, dog-coin family, frog/
# pepe meta, political meta, etc.) and tracks how many similar tokens the
# bot has caught recently. Three AI-agent coins launching in the last couple
# hours is a different, arguably stronger signal than any one keyword match
# — it means an entire category is rotating hot, not just one token.

META_CATEGORIES_FILE      = "meta_categories.txt"
META_HEAT_WINDOW_HOURS    = 3    # how far back to look when counting recent
                                  # same-category catches
META_HEAT_MIN_COUNT       = 2    # this many (or more) recent catches in the
                                  # same category = "heating up"

_meta_categories_cache: dict = {"data": {}, "fetched_at": 0.0}
META_CATEGORIES_REFRESH_SEC = 60   # re-read the file at most this often

_meta_alert_history: list = []   # [{"category": str, "timestamp": float}, ...]


def load_meta_categories() -> dict:
    """Parses meta_categories.txt into {category_name: set(keywords)}.
    Re-reads the file periodically (not on every single call) so you can
    edit it anytime without restarting, without hammering the disk every
    30 seconds either."""
    now = time.time()
    if now - _meta_categories_cache["fetched_at"] < META_CATEGORIES_REFRESH_SEC:
        return _meta_categories_cache["data"]

    categories: dict = {}
    current = None
    try:
        if os.path.exists(META_CATEGORIES_FILE):
            with open(META_CATEGORIES_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        current = line[1:-1].strip().lower()
                        categories[current] = set()
                    elif current:
                        categories[current].add(line.lower())
    except Exception as e:
        log.error(f"Meta categories file read error: {e}")

    _meta_categories_cache["data"] = categories
    _meta_categories_cache["fetched_at"] = now
    return categories


def classify_token_meta(text_blob: str, categories: dict) -> list:
    """Returns a list of category names this token's text matches.
    Same 3+ character minimum as trending keywords, same reasoning —
    avoids short/generic words causing false matches."""
    haystack = text_blob.lower()
    matched = []
    for category, keywords in categories.items():
        if any(len(kw) >= 3 and kw in haystack for kw in keywords):
            matched.append(category)
    return matched


def record_meta_alert(categories_matched: list) -> None:
    """Logs this alert's matched categories into the rolling history, used
    to compute how 'hot' a category currently is."""
    now = time.time()
    for cat in categories_matched:
        _meta_alert_history.append({"category": cat, "timestamp": now})

    # Prune anything older than the heat window while we're here, so this
    # list doesn't grow forever over a long-running session
    cutoff = now - (META_HEAT_WINDOW_HOURS * 3600)
    _meta_alert_history[:] = [e for e in _meta_alert_history if e["timestamp"] >= cutoff]


def get_meta_heat(categories_matched: list) -> dict:
    """Returns {category: recent_count} for each matched category, counting
    only entries within the rolling window (not including the current one
    being processed, since record_meta_alert hasn't been called yet at the
    point this is normally checked)."""
    now = time.time()
    cutoff = now - (META_HEAT_WINDOW_HOURS * 3600)
    heat = {}
    for cat in categories_matched:
        heat[cat] = sum(
            1 for e in _meta_alert_history
            if e["category"] == cat and e["timestamp"] >= cutoff
        )
    return heat




# ─── FILTERS ─────────────────────────────────────────────────────────────────

def passes_dex_filters(pair: dict) -> tuple[bool, str]:
    liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
    volume_5m = pair.get("volume", {}).get("m5", 0) or 0
    buys_5m   = pair.get("txns", {}).get("m5", {}).get("buys", 0) or 0
    sells_5m  = pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0
    txns_5m   = buys_5m + sells_5m

    created_at = pair.get("pairCreatedAt")
    if created_at:
        age_mins = (time.time() * 1000 - created_at) / 60_000
        if age_mins > MAX_TOKEN_AGE_MINS:
            return False, f"Too old ({age_mins:.0f} mins)"
        if age_mins < MIN_TOKEN_AGE_MINS:
            return False, f"Too new ({age_mins:.1f} mins) — letting early snipers clear"
    else:
        return False, "No creation timestamp"

    if liquidity < MIN_LIQUIDITY_USD:
        return False, f"Liquidity too low (${liquidity:,.0f})"
    if liquidity > MAX_LIQUIDITY_USD:
        return False, f"Liquidity too high (${liquidity:,.0f})"

    if volume_5m < MIN_VOLUME_5M_USD:
        return False, f"5m volume too low (${volume_5m:,.0f})"
    if txns_5m < MIN_TXNS_5M:
        return False, f"Too few 5m txns ({txns_5m})"

    if txns_5m > 0:
        ratio = buys_5m / txns_5m
        if ratio < MIN_BUY_SELL_RATIO:
            return False, f"Too sell-heavy ({buys_5m}B/{sells_5m}S = {ratio:.0%} buys)"

    # NEW: market cap range — keeps you in the "still early" zone
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    if mcap < MIN_MARKET_CAP_USD:
        return False, f"Market cap too low (${mcap:,.0f})"
    if mcap > MAX_MARKET_CAP_USD:
        return False, f"Market cap too high (${mcap:,.0f}) — already past early stage"

    # NEW: 5m volume relative to market cap — catches real momentum,
    # scales properly with token size unlike a flat dollar minimum
    if mcap > 0:
        vol_ratio = volume_5m / mcap
        if vol_ratio < MIN_VOL5M_TO_MCAP_RATIO:
            return False, f"5m volume too low relative to mcap ({vol_ratio:.0%} of mcap)"

    # NEW: reject if it's already pumped hard before we even saw it
    change_h1 = pair.get("priceChange", {}).get("h1", 0) or 0
    if change_h1 > MAX_PRICE_CHANGE_H1_PCT:
        return False, f"Already up {change_h1:.0f}% in 1h — too late"

    return True, ""


def passes_prebond_filter(pair: dict) -> tuple[bool, str]:
    """NEW: only pass tokens still on the pump.fun bonding curve (not yet
    migrated to Raydium). See the config note at ONLY_PRE_BOND for how to
    verify/adjust PRE_BOND_DEX_IDS against live data."""
    if not ONLY_PRE_BOND:
        return True, ""
    dex_id = (pair.get("dexId") or "").lower()
    if dex_id not in PRE_BOND_DEX_IDS:
        return False, f"Already bonded/migrated (dexId='{dex_id}')"
    return True, ""


def passes_rugcheck_filters(sec: dict) -> tuple[bool, str]:
    if sec["score"] > MAX_RUGCHECK_SCORE:
        return False, f"Rugcheck score too high ({sec['score']})"
    if REQUIRE_MINT_REVOKED and not sec["mint_revoked"]:
        return False, "Mint authority NOT revoked"
    if REQUIRE_FREEZE_DISABLED and not sec["freeze_revoked"]:
        return False, "Freeze authority NOT disabled"
    if REQUIRE_LP_BURNED and not sec["lp_burned"]:
        return False, "LP not burned"
    if REQUIRE_TOP10_CHECK and sec["top10_pct"] is not None and sec["top10_pct"] > MAX_TOP10_HOLDER_PCT:
        return False, f"Top 10 holders = {sec['top10_pct']:.1f}%"
    if sec["dev_pct"] is not None and sec["dev_pct"] > MAX_DEV_WALLET_PCT:
        return False, f"Dev holds {sec['dev_pct']:.1f}%"
    if REJECT_BUNDLED:
        if sec["bundled_pct"] is not None:
            if sec["bundled_pct"] > MAX_BUNDLE_PCT:
                return False, f"Bundled buys too high ({sec['bundled_pct']:.1f}%)"
        elif sec["is_bundled"]:
            return False, "Bundled launch detected (no exact % available)"
    if sec["sniper_count"] > MAX_SNIPER_COUNT:
        return False, f"Too many snipers ({sec['sniper_count']})"

    # NEW: catch anything else Rugcheck flagged as dangerous that wasn't
    # individually named above (copycat metadata, LP risk, etc.)
    if REJECT_DANGER_RISKS:
        for risk in sec["risks"]:
            level = (risk.get("level") or "").lower()
            if level in DANGER_RISK_LEVELS:
                return False, f"Danger risk flagged: {risk.get('name', '?')} ({level})"

    # NEW: minimum holder count, if Rugcheck reported one
    if sec["holders"] is not None and sec["holders"] < MIN_HOLDERS:
        return False, f"Too few holders ({sec['holders']})"

    return True, ""


def passes_lore_filters(sec: dict) -> tuple[bool, str]:
    """
    NEW logic: passes if the token has EITHER
      a) at least one linked social account (X/Telegram/website/Discord), OR
      b) a real written description (its own lore blurb), even with zero
         social accounts attached.
    This stops auto-rejecting tokens that have genuine lore/story but no
    dedicated social page yet.
    """
    if not REQUIRE_AT_LEAST_ONE_SOCIAL:
        return True, ""

    has_social = sec["lore_score"] >= MIN_LORE_SCORE
    desc       = (sec.get("description") or "").strip()
    has_desc   = len(desc) >= MIN_DESCRIPTION_LENGTH

    if has_social or has_desc:
        return True, ""

    return False, "No socials AND no real description found — no lore, no send"


def build_lore_summary(sec: dict) -> str:
    """
    Builds a compact lore panel for the alert message.
    Shows socials found, description snippet, and a quick vibe rating
    so you can eyeball it in 5 seconds.
    """
    lines = []

    # Social links
    if sec["twitter"]:
        lines.append(f"  🐦 [X/Twitter]({sec['twitter']})")
    else:
        lines.append(f"  🐦 X/Twitter: ❌ none")

    if sec["telegram"]:
        lines.append(f"  ✈️ [Telegram]({sec['telegram']})")

    if sec["website"]:
        lines.append(f"  🌐 [Website]({sec['website']})")

    if sec["discord"]:
        lines.append(f"  💬 [Discord]({sec['discord']})")

    # Lore vibe rating
    score = sec["lore_score"]
    has_x = sec["has_twitter"]
    desc  = (sec.get("description") or "").strip()
    has_desc = len(desc) >= MIN_DESCRIPTION_LENGTH

    if score >= 3 and has_x:
        vibe = "🔥 Strong lore — multiple socials + X"
    elif score >= 2 and has_x:
        vibe = "✅ Decent lore — X present"
    elif score >= 2:
        vibe = "🟡 Some lore — no X but other socials"
    elif score == 1 and has_x:
        vibe = "🟡 Thin lore — X only, check manually"
    elif score == 1:
        vibe = "⚠️ Bare minimum — one social, no X"
    elif score == 0 and has_desc:
        vibe = "📝 No socials, but real description — story-driven, check manually"
    else:
        vibe = "❌ No lore detected"

    lines.append(f"\n  *Vibe:* {vibe}")

    # Description snippet (first 120 chars)
    desc = sec.get("description", "").strip()
    if desc:
        snippet = desc[:120] + ("..." if len(desc) > 120 else "")
        lines.append(f"  📝 _{snippet}_")
    else:
        lines.append(f"  📝 _No description found — check manually_")

    return "\n".join(lines)

# ─── DIP DETECTION ───────────────────────────────────────────────────────────

def detect_dip(token_address: str, current_price: float) -> Optional[dict]:
    state = tracked_tokens.get(token_address)
    if not state:
        return None

    price_high = state.get("price_high", current_price)
    if current_price > price_high:
        state["price_high"] = current_price
        state["alerted_dips"] = set()
        return None

    initial_price = state.get("initial_price", current_price)
    pump_pct = ((price_high - initial_price) / initial_price * 100) if initial_price else 0
    if pump_pct < PUMP_MIN_PCT:
        return None

    drop_pct = (price_high - current_price) / price_high * 100
    for threshold in reversed(DIP_THRESHOLDS):
        key = f"dip_{threshold['pct']}"
        if drop_pct >= threshold["pct"] and key not in state["alerted_dips"]:
            # NEW: firing this tier also marks every lower tier as covered —
            # a 42% drop already implies the 20% and 30% marks were crossed
            # too, so there's no reason to separately "discover" and fire
            # those lower labels later once we've already alerted the
            # bigger drop. This was the source of "mild dip after hard dip."
            for lower in DIP_THRESHOLDS:
                if lower["pct"] <= threshold["pct"]:
                    state["alerted_dips"].add(f"dip_{lower['pct']}")
            return {
                "label":         threshold["label"],
                "drop_pct":      drop_pct,
                "price_high":    price_high,
                "current_price": current_price,
                "pump_pct":      pump_pct,
            }
    return None


def detect_volume_spike(pair: dict) -> bool:
    vol_5m = pair.get("volume", {}).get("m5", 0) or 0
    vol_1h = pair.get("volume", {}).get("h1", 0) or 0
    avg_5m = vol_1h / 12 if vol_1h else 0
    return avg_5m > 0 and vol_5m >= avg_5m * VOLUME_SPIKE_RATIO


def detect_dip_reentry(token_address: str, current_price: float, vol_spike: bool) -> bool:
    """Returns True the FIRST time a token shows the 'dipped from its peak,
    found a bottom, and is now bouncing back with fresh volume' pattern.
    Only fires once per token.

    No fixed drop-percentage window — dip depth varies too much per coin
    to gate on that. Instead, the bounce-off-low bar itself is stricter or
    looser depending on how strong the token's lore looked at launch (a
    trending-keyword match = easier bar to clear, no lore signal = needs a
    bigger bounce to prove itself)."""
    if not ENABLE_DIP_REENTRY_ALERTS:
        return False

    state = tracked_tokens.get(token_address)
    if not state or state.get("alerted_reentry"):
        return False

    peak = state.get("price_high", 0)
    if not peak:
        return False

    # Track the lowest price seen since the peak. As long as price keeps
    # falling, we're still "finding the bottom" — not a re-entry yet.
    low_since_peak = state.get("price_low_since_peak")
    if low_since_peak is None or current_price < low_since_peak:
        state["price_low_since_peak"] = current_price
        return False

    if low_since_peak <= 0 or current_price <= low_since_peak:
        return False  # no dip happened yet, or hasn't started bouncing

    bounce_off_low_pct = (current_price - low_since_peak) / low_since_peak * 100

    min_bounce = (
        DIP_REENTRY_MIN_BOUNCE_STRONG_LORE
        if state.get("had_trending_lore")
        else DIP_REENTRY_MIN_BOUNCE_WEAK_LORE
    )
    if bounce_off_low_pct < min_bounce:
        return False

    if not vol_spike:
        return False

    state["alerted_reentry"] = True
    return True


def _dynamic_thresholds(current_multiple: float) -> list:
    """Explicit milestone list, plus auto-continuing steps beyond the
    largest explicit one, so huge runs keep alerting without needing the
    config list edited by hand."""
    thresholds = list(MULTIPLIER_THRESHOLDS)
    biggest = max(MULTIPLIER_THRESHOLDS)
    step = biggest
    next_level = biggest + step
    while next_level <= current_multiple + step:
        thresholds.append(next_level)
        next_level += step
    return sorted(set(thresholds))


def detect_multiplier(token_address: str, current_price: float) -> list:
    """Returns a list of newly-crossed multiplier milestones (e.g. [2, 3])
    since the token was first alerted. Each milestone only returns once."""
    state = tracked_tokens.get(token_address)
    if not state:
        return []
    initial_price = state.get("initial_price", 0)
    if not initial_price:
        return []

    multiple = current_price / initial_price
    if multiple < min(MULTIPLIER_THRESHOLDS):
        return []

    newly_hit = []
    for t in _dynamic_thresholds(multiple):
        key = f"x{t}"
        if multiple >= t and key not in state["alerted_multiples"]:
            state["alerted_multiples"].add(key)
            newly_hit.append(t)
    return newly_hit

# ─── TELEGRAM MESSAGES ───────────────────────────────────────────────────────

def security_badge(value: bool, good_is_true: bool = True) -> str:
    return "✅" if (value if good_is_true else not value) else "❌"


def format_launch_alert(pair: dict, sec: dict, trend: dict, meta_info: dict = None) -> str:
    base    = pair.get("baseToken", {})
    liq     = pair.get("liquidity", {}).get("usd", 0) or 0
    vol5m   = pair.get("volume", {}).get("m5", 0) or 0
    price   = pair.get("priceUsd", "?")
    mc      = pair.get("marketCap") or pair.get("fdv") or 0
    buys5m  = pair.get("txns", {}).get("m5", {}).get("buys", 0) or 0
    sells5m = pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0

    created_at = pair.get("pairCreatedAt", 0)
    age_mins   = (time.time() * 1000 - created_at) / 60_000 if created_at else 0

    addr    = base.get("address", "")
    dex_url = pair.get("url", f"https://dexscreener.com/solana/{addr}")
    birdeye = f"https://birdeye.so/token/{addr}?chain=solana"
    photon  = f"https://photon-sol.tinyastro.io/en/lp/{pair.get('pairAddress','')}"
    ruglink = f"https://rugcheck.xyz/tokens/{addr}"
    dex_id  = pair.get("dexId", "?")

    score = sec["score"]
    risk_label = (
        "🟢 Low Risk"   if score < 200 else
        "🟡 Moderate"   if score < 500 else
        "🟠 Elevated"
    )

    top10_str = f"{sec['top10_pct']:.1f}%" if sec["top10_pct"] is not None else "?"
    dev_str   = f"{sec['dev_pct']:.1f}%"  if sec["dev_pct"]   is not None else "?"
    lp_status = (
        "🔥 Burned"   if sec["lp_burned"] else
        "🔒 Locked"   if sec["lp_locked"] else
        "⚠️ Unlocked"
    )
    meta_warn   = "\n  ⚠️ Mutable metadata!" if sec["mutable_meta"] else ""
    bundle_str = (
        f"{sec['bundled_pct']:.1f}%"
        if sec.get("bundled_pct") is not None
        else security_badge(sec["is_bundled"], good_is_true=False)
    )
    lore_block  = build_lore_summary(sec)

    trend_line = ""
    all_hits = trend.get("manual_hits", set()) | trend.get("auto_hits", set())
    if all_hits:
        watchlist = ", ".join(sorted(trend.get("manual_hits", set())))
        auto      = ", ".join(sorted(trend.get("auto_hits", set())))
        parts = []
        if watchlist:
            parts.append(f"watchlist: {watchlist}")
        if auto:
            parts.append(f"auto-trending: {auto}")
        trend_line = f"🔥🔥 *TRENDING NARRATIVE MATCH* — {' | '.join(parts)}\n\n"

    narrative_meta_line = ""
    if meta_info and meta_info.get("categories"):
        cats = meta_info["categories"]
        heat = meta_info.get("heat", {})
        parts = []
        for c in cats:
            h = heat.get(c, 0)
            heat_tag = f" (🔥 {h} similar caught recently — heating up)" if h >= META_HEAT_MIN_COUNT else ""
            parts.append(f"{c}{heat_tag}")
        narrative_meta_line = f"🎭 *Meta:* {', '.join(parts)}\n\n"

    return (
        f"🚀 *NEW LAUNCH ALERT* 🚀\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{trend_line}"
        f"{narrative_meta_line}"
        f"*{base.get('name','?')}* `{base.get('symbol','?')}`\n"
        f"📋 `{addr}`\n\n"
        f"💰 Price: `${price}`\n"
        f"🏦 Liquidity: `${liq:,.0f}`\n"
        f"📊 Market Cap: `${mc:,.0f}`\n"
        f"📈 5m Volume: `${vol5m:,.0f}`\n"
        f"🔄 5m Txns: 🟢 {buys5m} buys / 🔴 {sells5m} sells\n"
        f"⏱ Age: `{age_mins:.1f} mins`\n"
        f"🧬 DEX: `{dex_id}`\n\n"
        f"🛡 *Security — Score: {score} ({risk_label})*\n"
        f"  Mint Revoked: {security_badge(sec['mint_revoked'])}\n"
        f"  Freeze Disabled: {security_badge(sec['freeze_revoked'])}\n"
        f"  LP: {lp_status}\n"
        f"  Top 10 Holders: `{top10_str}`\n"
        f"  Dev Wallet: `{dev_str}` of supply\n"
        f"  Bundled Launch: {bundle_str}\n"
        f"  Sniper Wallets: `{sec['sniper_count']}`\n"
        f"{meta_warn}\n\n"
        f"🎭 *Lore Check*\n"
        f"{lore_block}\n\n"
        f"🔗 [DEXScreener]({dex_url}) | [Birdeye]({birdeye}) | [Photon]({photon}) | [Rugcheck]({ruglink})"
    )


def format_dip_alert(pair: dict, dip: dict, vol_spike: bool) -> str:
    base    = pair.get("baseToken", {})
    liq     = pair.get("liquidity", {}).get("usd", 0) or 0
    vol5m   = pair.get("volume", {}).get("m5", 0) or 0
    addr    = base.get("address", "")
    dex_url = pair.get("url", f"https://dexscreener.com/solana/{addr}")
    birdeye = f"https://birdeye.so/token/{addr}?chain=solana"
    spike   = "\n📣 *VOLUME SPIKE alongside dip — possible bounce!*" if vol_spike else ""

    return (
        f"{dip['label']} *DIP ALERT*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*{base.get('name','?')}* `{base.get('symbol','?')}`\n"
        f"📋 `{addr}`\n\n"
        f"📉 Drop from high: `{dip['drop_pct']:.1f}%`\n"
        f"🏔 Local High: `${dip['price_high']:.8f}`\n"
        f"💲 Now: `${dip['current_price']:.8f}`\n"
        f"🚀 Pre-dip Pump: `+{dip['pump_pct']:.0f}%`\n"
        f"🏦 Liquidity: `${liq:,.0f}`\n"
        f"📈 5m Volume: `${vol5m:,.0f}`\n"
        f"{spike}\n\n"
        f"🔗 [DEXScreener]({dex_url}) | [Birdeye]({birdeye})"
    )

# ─── KEEP-ALIVE WEB SERVER (NEW, for Render free tier) ─────────────────────
# Render's free Web Services sleep after 15 minutes without incoming HTTP
# traffic. This bot doesn't naturally receive any (it just polls APIs on a
# timer), so this tiny server gives something for an external "pinger"
# (UptimeRobot, free) to hit every ~10 minutes and keep the service awake.
# Locally on your own laptop this piece is harmless — it just quietly opens
# a local port you'll never look at.

async def health(request):
    return web.Response(text="Sniper bot is alive ✅")

async def start_webserver():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))  # Render sets PORT automatically
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"🌐 Keep-alive web server listening on port {port}")

def format_dip_reentry_alert(pair: dict, state: dict, current_price: float, current_mcap: float) -> str:
    base    = pair.get("baseToken", {})
    addr    = base.get("address", "")
    dex_url = pair.get("url", f"https://dexscreener.com/solana/{addr}")
    birdeye = f"https://birdeye.so/token/{addr}?chain=solana"

    peak         = state.get("price_high", 0)
    low          = state.get("price_low_since_peak", 0)
    initial_mcap = state.get("initial_mcap", 0)
    drop_pct     = (peak - low) / peak * 100 if peak else 0
    bounce_pct   = (current_price - low) / low * 100 if low else 0
    vol5m        = pair.get("volume", {}).get("m5", 0) or 0
    lore_tier    = "🔥 strong lore — easier bar" if state.get("had_trending_lore") else "🟡 no lore signal — needed a bigger bounce"

    return (
        f"🔁 *DIP RE-ENTRY* 🔁\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*{base.get('name','?')}* `{base.get('symbol','?')}`\n"
        f"📋 `{addr}`\n\n"
        f"Pumped, dipped *{drop_pct:.0f}%* from peak, now bouncing "
        f"*+{bounce_pct:.0f}%* off the low — with fresh volume coming in.\n"
        f"Lore: {lore_tier}\n\n"
        f"📊 Market Cap: `${initial_mcap:,.0f}` (start) → `${current_mcap:,.0f}` (now)\n"
        f"💲 Price: peak `${peak:.8f}` → low `${low:.8f}` → now `${current_price:.8f}`\n"
        f"📈 5m Volume: `${vol5m:,.0f}`\n\n"
        f"🔗 [DEXScreener]({dex_url}) | [Birdeye]({birdeye})"
    )


def format_hourly_report(snapshot: list) -> str:
    """snapshot is a list of dicts: {symbol, gain_pct, mcap, url, address}
    collected during THIS hour's tracked-tokens scan (current-moment view).

    NEW: this now also pulls from the permanent, never-cleared records
    (_total_tokens_caught, _all_time_2x_log) so the report shows the full
    picture — everything ever caught, everything that ever hit 2x+, not
    just whatever's currently sitting above the bar at the exact moment
    the report happens to run."""
    this_hour_winners = [s for s in snapshot if s["gain_pct"] >= HOURLY_REPORT_MIN_GAIN_PCT]
    this_hour_winners.sort(key=lambda s: s["gain_pct"], reverse=True)
    this_hour_winners = this_hour_winners[:HOURLY_REPORT_MAX_LISTED]

    header = (
        f"📊 *HOURLY REPORT*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Total caught since bot started: `{_total_tokens_caught}`\n"
        f"🚀 Total 2x+ hits since bot started: `{len(_all_time_2x_log)}`\n"
        f"👀 Currently tracking: `{len(snapshot)}` tokens\n\n"
    )

    # ── All-time 2x+ log ──────────────────────────────────────────────────
    alltime_section = "🏆 *ALL-TIME 2x+ ACHIEVERS*\n"
    if not _all_time_2x_log:
        alltime_section += "None yet.\n"
    else:
        # Show the best hit per token (not every single milestone it passed
        # through), most recent/highest first, capped so it stays readable
        best_per_token = {}
        for entry in _all_time_2x_log:
            addr = entry["address"]
            if addr not in best_per_token or entry["threshold"] > best_per_token[addr]["threshold"]:
                best_per_token[addr] = entry
        ranked = sorted(best_per_token.values(), key=lambda e: e["threshold"], reverse=True)
        for i, e in enumerate(ranked[:HOURLY_REPORT_MAX_LISTED], 1):
            alltime_section += f"{i}. *{e['symbol']}* — `{e['threshold']:g}x` (mcap ${e['mcap']:,.0f}) [chart]({e['url']})\n"
        if len(ranked) > HOURLY_REPORT_MAX_LISTED:
            alltime_section += f"...+{len(ranked) - HOURLY_REPORT_MAX_LISTED} more\n"

    # ── This hour's snapshot ──────────────────────────────────────────────
    hour_section = f"\n📈 *THIS HOUR* (currently up {HOURLY_REPORT_MIN_GAIN_PCT}%+)\n"
    if not this_hour_winners:
        hour_section += "Nothing cleared the bar this hour — quiet stretch, not necessarily a problem."
    else:
        for i, s in enumerate(this_hour_winners, 1):
            hour_section += (
                f"{i}. *{s['symbol']}* — +{s['gain_pct']:.0f}% "
                f"(mcap ${s['mcap']:,.0f}) [chart]({s['url']})\n"
            )

    return header + alltime_section + hour_section


def format_daily_top_performers_report() -> str:
    """Top tokens by peak multiplier ('Xs') reached in the last 24 hours.
    Built entirely from the persistent _all_time_2x_log (which already
    records every milestone hit with a timestamp) — this only READS that
    list, never modifies or prunes it, so the existing hourly report's own
    all-time section is completely unaffected by this function existing."""
    cutoff = time.time() - 24 * 3600
    recent = [e for e in _all_time_2x_log if e["timestamp"] >= cutoff]

    header = (
        f"🗓️ *24-HOUR TOP PERFORMERS*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    if not recent:
        return header + "No tokens hit 2x+ in the last 24 hours — quiet stretch, not necessarily a problem."

    # Best (highest) milestone per token in the window, not every single
    # threshold it passed through along the way — same approach as the
    # hourly report's all-time section.
    best_per_token = {}
    for entry in recent:
        addr = entry["address"]
        if addr not in best_per_token or entry["threshold"] > best_per_token[addr]["threshold"]:
            best_per_token[addr] = entry

    ranked = sorted(best_per_token.values(), key=lambda e: e["threshold"], reverse=True)

    body = ""
    for i, e in enumerate(ranked[:DAILY_REPORT_MAX_LISTED], 1):
        body += (
            f"{i}. *{e['symbol']}* — `{e['threshold']:g}x` "
            f"(mcap ${e['mcap']:,.0f}) [chart]({e['url']})\n"
        )
    if len(ranked) > DAILY_REPORT_MAX_LISTED:
        body += f"...+{len(ranked) - DAILY_REPORT_MAX_LISTED} more hit 2x+ in the last 24h\n"

    body += f"\n📊 Total unique tokens that hit 2x+ in the last 24h: `{len(best_per_token)}`"

    return header + body


def format_multiplier_alert(pair: dict, threshold: float, state: dict, current_price: float, current_mcap: float) -> str:
    base    = pair.get("baseToken", {})
    addr    = base.get("address", "")
    dex_url = pair.get("url", f"https://dexscreener.com/solana/{addr}")
    birdeye = f"https://birdeye.so/token/{addr}?chain=solana"

    initial_price = state.get("initial_price", 0)
    initial_mcap  = state.get("initial_mcap", 0)

    emoji = "🚀" if threshold < 10 else "🌕" if threshold < 100 else "🪐"

    return (
        f"{emoji} *{threshold:g}x REACHED* {emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*{base.get('name','?')}* `{base.get('symbol','?')}`\n"
        f"📋 `{addr}`\n\n"
        f"📊 Market Cap: `${initial_mcap:,.0f}` → `${current_mcap:,.0f}`\n"
        f"💲 Price: `${initial_price:.8f}` → `${current_price:.8f}`\n\n"
        f"🔗 [DEXScreener]({dex_url}) | [Birdeye]({birdeye})"
    )

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

async def process_new_token(
    session: aiohttp.ClientSession,
    bot: Bot,
    token_address: str,
    manual_kw: set,
    auto_kw: set,
) -> None:
    """Runs every filter step for one newly-discovered token and sends the
    launch alert if it passes everything. Pulled out into its own function
    so the caller can wrap a single token's processing in a try/except —
    if any one token's data causes an unexpected error, only that token is
    skipped, instead of it taking down the whole bot."""

    # Step 1: DEX filters (fast, no extra API call)
    pairs = await fetch_token_pairs(session, token_address)
    if not pairs:
        return
    pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)

    dex_ok, dex_reason = passes_dex_filters(pair)
    if not dex_ok:
        log.info(f"DEX fail [{token_address[:8]}]: {dex_reason}")
        return

    # Step 2: Pre-bond filter
    prebond_ok, prebond_reason = passes_prebond_filter(pair)
    if not prebond_ok:
        log.info(
            f"Pre-bond fail [{token_address[:8]}]: {prebond_reason} "
            f"(dexId='{pair.get('dexId')}')"
        )
        return

    # Step 3: Rugcheck (security + lore data)
    report = await fetch_rugcheck(session, token_address)
    if not report:
        log.info(f"No Rugcheck data for {token_address[:8]}, skipping")
        return

    sec = parse_rugcheck(report)

    # Step 4: Security filter
    rug_ok, rug_reason = passes_rugcheck_filters(sec)
    if not rug_ok:
        log.info(f"Security fail [{token_address[:8]}]: {rug_reason}")
        return

    # Step 5: Lore filter
    lore_ok, lore_reason = passes_lore_filters(sec)
    if not lore_ok:
        log.info(f"Lore fail [{token_address[:8]}]: {lore_reason}")
        return

    # Step 6: Trending lore check (informational, or hard filter if
    # TRENDING_LORE_ONLY is True)
    trend = check_trending_lore(pair, sec, manual_kw, auto_kw)
    if TRENDING_LORE_ONLY and not trend["is_trending"]:
        log.info(f"Trending fail [{token_address[:8]}]: no keyword match")
        return

    # NEW: meta classification — what narrative category (if any) does
    # this token belong to, and how "hot" has that category been recently
    base_for_meta = pair.get("baseToken", {})
    meta_text_blob = " ".join([
        base_for_meta.get("name", "") or "",
        base_for_meta.get("symbol", "") or "",
        sec.get("description", "") or "",
    ])
    meta_categories_dict = load_meta_categories()
    matched_categories    = classify_token_meta(meta_text_blob, meta_categories_dict)
    meta_heat             = get_meta_heat(matched_categories)
    meta_info = {"categories": matched_categories, "heat": meta_heat}
    if matched_categories:
        record_meta_alert(matched_categories)

    # ✅ All filters passed
    price_now = float(pair.get("priceUsd", 0) or 0)
    mcap_now  = pair.get("marketCap") or pair.get("fdv") or 0
    liq_now   = pair.get("liquidity", {}).get("usd", 0) or 0
    tracked_tokens[token_address] = {
        "first_seen":          time.time(),
        "initial_price":       price_now,
        "price_high":          price_now,
        "price_low_since_peak": None,
        "alerted_reentry":     False,
        "initial_liquidity":   liq_now,   # NEW: real value captured immediately,
                                           # this path has it on hand already
        "presumed_rugged":     False,     # NEW
        "had_trending_lore":   bool(trend["is_trending"]),
        "initial_mcap":        mcap_now,
        "alerted_dips":        set(),
        "alerted_multiples":   set(),
        "pair_address":        pair.get("pairAddress"),
    }

    sym = pair.get("baseToken", {}).get("symbol", "?")
    log.info(
        f"🚀 ALERT: {sym} ({token_address[:8]}...) | "
        f"dexId: {pair.get('dexId')} | "
        f"Score: {sec['score']} | Socials: {sec['lore_score']} | "
        f"X: {'yes' if sec['has_twitter'] else 'no'} | "
        f"Trending: {'yes' if trend['is_trending'] else 'no'}"
    )

    global _total_tokens_caught
    _total_tokens_caught += 1

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=format_launch_alert(pair, sec, trend, meta_info),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def run_bot():
    global _last_hourly_report, _last_24h_report
    _last_hourly_report = time.time()  # NEW: start the hourly clock now,
                                        # not at 0 — otherwise the first
                                        # report would fire immediately
    _last_24h_report = time.time()     # NEW: same idea for the 24h report

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    log.info("🤖 Solana Memecoin Sniper Bot v5 started")

    # NEW: this startup message used to be unguarded — if Telegram was
    # slow to respond even once, right here, the whole bot crashed before
    # ever reaching the monitoring loop. Now a failure here is logged and
    # the bot proceeds to actually run regardless.
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "🤖 *Solana Sniper Bot v5 is live!*\n"
                "Data: DEXScreener + Rugcheck.xyz + CoinGecko trending\n"
                "Filters: DEX → Pre-bond → Security → Lore → Trending ✅\n"
                "Monitoring new launches, security checks, lore & dips..."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as e:
        log.error(f"Startup message failed to send (continuing anyway): {e}")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Refresh trending keyword lists once per loop (cheap; file
                # read + rate-limited CoinGecko call internally)
                manual_kw, auto_kw = await get_trending_keywords(session)

                # ── 1. NEW LAUNCH SCAN ────────────────────────────────────
                profiles = await fetch_new_solana_profiles(session)

                for profile in profiles:
                    token_address = profile.get("tokenAddress")
                    if not token_address or token_address in tracked_tokens:
                        continue

                    # NEW: wrapping each token's processing individually —
                    # this is a second safety layer. If any single token's
                    # data causes an unexpected error, we skip just that
                    # token and keep the bot running, instead of the whole
                    # process crashing.
                    try:
                        await process_new_token(
                            session, bot, token_address, manual_kw, auto_kw
                        )
                    except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as e:
                        log.error(f"Token processing error [{token_address[:8]}]: {e}")
                        continue

                # ── 1b. GMGN LAUNCH SCAN (NEW, second source) ─────────────
                if ENABLE_GMGN_SOURCE:
                    gmgn_tokens = await fetch_gmgn_trenches("new_creation")
                    for gtoken in gmgn_tokens:
                        addr = gtoken.get("address")
                        if not addr or addr in tracked_tokens:
                            continue
                        try:
                            await process_new_gmgn_token(bot, gtoken)
                        except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as e:
                            log.error(f"GMGN token processing error [{addr[:8]}]: {e}")
                            continue

                # ── 2. DIP SCAN ───────────────────────────────────────────
                stale = []
                hourly_snapshot = []  # NEW: collected fresh each cycle for the hourly report
                for token_address, state in tracked_tokens.items():
                    if time.time() - state["first_seen"] > 6 * 3600:
                        stale.append(token_address)
                        continue

                    # NEW: same per-token isolation as the launch scan above —
                    # one tracked token's hiccup (e.g. a failed send_message)
                    # no longer skips every other tracked token in this cycle.
                    try:
                        pairs = await fetch_token_pairs(session, token_address)
                        if not pairs:
                            continue

                        pair      = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
                        price_now = float(pair.get("priceUsd", 0) or 0)
                        if not price_now:
                            continue

                        # NEW: liquidity-collapse guard. Capture a baseline
                        # the first time we see real liquidity data for this
                        # token (covers both discovery paths — DEXScreener
                        # tokens already have this from launch, GMGN tokens
                        # get it set here on their first scan cycle since
                        # GMGN's own liquidity field isn't in USD).
                        liq_now = pair.get("liquidity", {}).get("usd", 0) or 0
                        if state.get("initial_liquidity") is None and liq_now > 0:
                            state["initial_liquidity"] = liq_now

                        initial_liq = state.get("initial_liquidity")
                        if initial_liq and not state.get("presumed_rugged"):
                            liq_drop_pct = (initial_liq - liq_now) / initial_liq * 100
                            if liq_drop_pct >= LIQUIDITY_RUG_DROP_PCT:
                                state["presumed_rugged"] = True
                                sym = pair.get("baseToken", {}).get("symbol", "?")
                                log.info(
                                    f"⚰️ Presumed rugged (liquidity -{liq_drop_pct:.0f}%): "
                                    f"{sym} ({token_address[:8]}...) — suppressing further alerts"
                                )

                        # If presumed rugged, skip all further alert logic
                        # for this token entirely — no more dip/reentry/
                        # multiplier messages on something already dead.
                        # It also stops counting toward the hourly report's
                        # "currently tracking" figures from this point on,
                        # which is fine — a rugged token was never going to
                        # clear the "winner" bar anyway.
                        if state.get("presumed_rugged"):
                            continue

                        # NEW: record this token's current standing for the
                        # hourly report, regardless of whether any alert
                        # fires this cycle
                        initial_price = state.get("initial_price", 0)
                        if initial_price:
                            gain_pct = (price_now - initial_price) / initial_price * 100
                            hourly_snapshot.append({
                                "symbol":  pair.get("baseToken", {}).get("symbol", "?"),
                                "gain_pct": gain_pct,
                                "mcap":    pair.get("marketCap") or pair.get("fdv") or 0,
                                "url":     pair.get("url", f"https://dexscreener.com/solana/{token_address}"),
                                "address": token_address,
                            })

                        # Peak tracking now runs unconditionally (not gated
                        # behind ENABLE_DIP_ALERTS) so dip re-entry detection
                        # keeps working even if you turn plain dip alerts off.
                        if price_now > state.get("price_high", 0):
                            state["price_high"] = price_now
                            state["alerted_dips"] = set()

                        dip       = detect_dip(token_address, price_now) if ENABLE_DIP_ALERTS else None
                        vol_spike = detect_volume_spike(pair)

                        # Multiplier milestone alerts (2x, 3x, 5x, 10x, ...)
                        mcap_now       = pair.get("marketCap") or pair.get("fdv") or 0
                        hit_thresholds = detect_multiplier(token_address, price_now)
                        for t in hit_thresholds:
                            sym = pair.get("baseToken", {}).get("symbol", "?")
                            log.info(f"📈 {t}x milestone hit: {sym} ({token_address[:8]}...)")

                            # NEW: record every 2x+ hit permanently — this is
                            # what lets the hourly report show "every play
                            # that's ever hit 2x," not just whatever's
                            # currently up 20%+ at the exact report moment
                            _all_time_2x_log.append({
                                "symbol":    sym,
                                "address":   token_address,
                                "threshold": t,
                                "mcap":      mcap_now,
                                "url":       pair.get("url", f"https://dexscreener.com/solana/{token_address}"),
                                "timestamp": time.time(),
                            })

                            await bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=format_multiplier_alert(pair, t, state, price_now, mcap_now),
                                parse_mode=ParseMode.MARKDOWN,
                                disable_web_page_preview=True,
                            )

                        # NEW: dip re-entry alert — pumped, dipped, now
                        # bouncing back with fresh volume
                        if detect_dip_reentry(token_address, price_now, vol_spike):
                            sym = pair.get("baseToken", {}).get("symbol", "?")
                            log.info(f"🔁 Dip re-entry: {sym} ({token_address[:8]}...)")
                            await bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=format_dip_reentry_alert(pair, state, price_now, mcap_now),
                                parse_mode=ParseMode.MARKDOWN,
                                disable_web_page_preview=True,
                            )

                        if dip:
                            sym = pair.get("baseToken", {}).get("symbol", "?")
                            log.info(f"{dip['label']} {sym}: -{dip['drop_pct']:.1f}%")
                            await bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=format_dip_alert(pair, dip, vol_spike),
                                parse_mode=ParseMode.MARKDOWN,
                                disable_web_page_preview=True,
                            )
                        # NEW: standalone "VOLUME SPIKE" alert removed per
                        # your call — volume info now only surfaces as
                        # confirmation inside dip alerts (already shown
                        # there) and dip re-entry alerts (which already
                        # require a volume spike to fire at all). No more
                        # separate volume-only message on its own.
                    except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as e:
                        log.error(f"Tracked token error [{token_address[:8]}]: {e}")
                        continue

                for key in stale:
                    del tracked_tokens[key]

                # ── 3. HOURLY REPORT (NEW) ─────────────────────────────────
                if time.time() - _last_hourly_report >= HOURLY_REPORT_INTERVAL_SEC:
                    if hourly_snapshot:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=format_hourly_report(hourly_snapshot),
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=True,
                        )
                    _last_hourly_report = time.time()

                # ── 4. 24-HOUR TOP PERFORMERS REPORT (NEW) ─────────────────
                # Independent of hourly_snapshot/tracked_tokens — reads only
                # the persistent _all_time_2x_log, so it works regardless of
                # the 6-hour tracked-tokens cleanup above.
                if time.time() - _last_24h_report >= DAILY_REPORT_INTERVAL_SEC:
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=format_daily_top_performers_report(),
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                    )
                    _last_24h_report = time.time()

            except (Exception, asyncio.CancelledError, asyncio.TimeoutError) as e:
                log.error(f"Main loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    async def main():
        # Runs the keep-alive web server and the sniper bot loop side by side
        await asyncio.gather(start_webserver(), run_bot())

    asyncio.run(main())

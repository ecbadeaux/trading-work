# data

Market data records, the Alpaca historical loader, the local bar store with its downloader,
delisted stocks, and the tick store. Paths are relative to this folder
(`trading-bot/src/trading_bot/data/`).

**Up:** [`../README.md`](../README.md)

## Records (`models.py`)

Plain frozen dataclasses with no alpaca-py dependency:

- `Bar(timestamp, open, high, low, close, volume, vwap)` is an intraday bar. `timestamp` is the
  bar's **start**, timezone-aware.
- `DailyBar(day, open, high, low, close, volume, adj_close)` holds raw prices plus the
  split-adjusted close. When `close / adj_close` changes inside a window, a split happened.
- `TradingDay(day, open, close)` is one regular session. Open and close are tz-aware US/Eastern
  and include early closes.
- `ET` is `ZoneInfo("America/New_York")`.

## Alpaca loader (`historical.py`)

`AlpacaHistoricalSource(stock_data, trading, feed="sip", cache_dir=None, chunk_size=200,
max_requests_per_minute=150)` implements the backtester's `DataSource` protocol.

| Method | What it requests |
|---|---|
| `trading_days(start, end)` | `TradingClient.get_calendar`. Alpaca's naive ET times are made tz-aware |
| `daily_bars(symbols, start, end)` | 1-day bars twice, **raw** and **split-adjusted**, merged into `DailyBar.adj_close`. Daily timestamps (midnight ET) are converted to ET dates |
| `opening_bars(symbols, day, minutes)` | Raw 1-minute bars in `[open, open + minutes)` |
| `session_bars(symbols, day)` | Raw 1-minute bars in `[open, close)` |

Behavior:

- **Feed:** SIP by default, because relative volume needs full-market volume. The Basic plan
  allows SIP history except for the last 15 minutes.
- **Raw intraday prices.** Simulated fills and per-share costs use prices as actually traded.
- **Requests:**
  - Symbols are chunked at `chunk_size` per request.
  - alpaca-py paginates internally and retries HTTP 429 and 504 (3 times, 3 s apart).
  - `_throttle()` spaces our requests to `max_requests_per_minute` (pages inside a request are
    not counted).
  - `.requests` counts the requests sent.
- `active_us_equities(trading)` returns today's active, tradable US equities on NYSE, NASDAQ,
  ARCA, AMEX or BATS. Because it is today's list, it carries **survivorship bias**. It reads
  the raw `/assets` JSON rather than `get_all_assets()`, because the SDK's exchange enum rejects
  the whole list when Alpaca adds an exchange it doesn't know (TXSE broke alpaca-py 0.44.0).
  `stocks_only=True` also drops exchange-traded products and funds. Alpaca has no ETF flag, so
  this is a heuristic: it drops primary listings on NYSE Arca and Cboe BZX (almost all ETPs) and
  names matching `FUND_NAME` (ETF, ETN, Fund, iShares, ProShares, SPDR, Direxion, "Trust,
  Series"). On 2026-11-01 it kept 7,130 of 13,189 symbols.

## Cache layout

Files are gzip-compressed JSON under `cache_dir` (the CLI default is `trading-bot/data-cache/`,
gitignored):

```
calendar/<start>_<end>.json.gz
daily/<feed>/<start>_<end>/<sha1 of symbol chunk>.json.gz
or<minutes>/<feed>/<YYYY-MM-DD>.json.gz      {"fetched": [...symbols], "bars": {sym: rows}}
session/<feed>/<YYYY-MM-DD>.json.gz          same shape
```

- **Intraday files record which symbols were fetched.** A later run only requests the missing
  ones, and the file is saved after every chunk, so an interrupted run resumes.
- **Deleting the cache is always safe**; it just costs refetching.
- Only cache data from completed days. The backtest CLI enforces this by requiring `--end` to be
  before today.

## Local store (`store.py`) and downloader (`download.py`)

`trading-bot-download` fills one SQLite file (default `trading-bot/data-store/bars.sqlite`,
gitignored) so backtests can run without the API. Data only; it never places orders.

**What it downloads** (each step skips what the store already holds, so re-running resumes):

1. The symbol list (`--universe stocks` by default) plus `--extra-symbols` (19 liquid ETFs by
   default: SPY, QQQ, IWM, DIA, TQQQ, SMH, the 11 sector SPDRs, GLD, TLT). The list is saved
   in the store and reused, so a resumed run does not drift; `--refresh-universe` re-reads it.
2. The calendar and daily bars (raw plus split-adjusted close) from 14 sessions before
   `--start` through `--end`. With `--include-delisted`, also the delisted stocks (below), once;
   later runs reuse the saved list unless `--refresh-delisted`.
3. A plan from the backtester's own pass 1 (`backtest.engine.daily_eligibility` and
   `needed_by_day`): every symbol-day a default-filter backtest asks for, plus every extra
   symbol every day. Stricter filters need a subset; looser ones (for example a lower
   `--min-price`) need symbol-days the store lacks, which `StoreSource` fetches on demand.
4. Full-session minute bars (raw, SIP) for the plan, `--chunk-size` symbols per request (25 x
   390 bars fits one 10,000-bar page).

**Rate limits.** `BarFetcher` requests one page at a time through the client's `get()` and
spaces every page to `--rpm` (default 180; the Basic plan allows 200 calls per minute).
alpaca-py's own paging would send follow-up pages unthrottled, and its retry (3 tries, 3 s
apart) is too short for a one-minute window, so HTTP 429/5xx and connection errors back off
5 s, 10 s, 20 s ... up to 120 s, with jitter, 8 times.

**Parallel requests.** One request takes about 0.7 s, so a single thread tops out near 80 per
minute. `--workers` (default 4) keeps that many requests in flight through `ordered_map`; the
fetcher's throttle is shared and thread-safe, so `--rpm` still caps the rate. Results are
written in order from the main thread.

Requests take an `asof` date that decides which company a ticker means. The default (today) maps
a current symbol to its company, including its history under former tickers (FB data under
META). Delisted stocks are fetched with `asof="-"`: whatever traded under the ticker.

**`--estimate`** runs steps 1–3 and prints the symbol-days, requests, GB and hours, then exits.
The GB figure uses `BYTES_PER_SYMBOL_DAY` (about 6.5 KB, measured from the old cache).

**Schema** (`BarStore`):

| Table | Contents |
|---|---|
| `meta` | `head_minutes`, `universe` (kind, symbols, date), `extra_symbols`, `calendar_range`, `delisted` (key: ticker, first and last day, sources), `delisted_info` (counts) |
| `calendar` | `day`, `open`, `close` (ISO, US/Eastern) |
| `daily` | One row per symbol: the stored window and a zlib-compressed JSON list of `[day, o, h, l, c, v, adj_close]` |
| `minute` | One row per (day, symbol): `head` (first `head_minutes`, default 30) and `rest`, each zlib-compressed JSON `[seconds after the open, o, h, l, c, v, vwap]`. Empty blobs mean "fetched, no bars" |

Opening ranges up to `head_minutes` decompress only the head, so a backtest touching ~1,000
symbols a day stays fast.

**Using it.** `trading-bot-backtest --store data-store/bars.sqlite` reads through `StoreSource`.
Anything the store lacks comes from Alpaca (and the normal `data-cache/`) unless `--offline`.
Store misses are logged and saved in `summary.json` (`store_misses`). `BarStore.universe(kind)`
gives the symbols of one of `UNIVERSES`:

| Universe | Symbols |
|---|---|
| `stocks` | The saved symbol list |
| `store` | Plus the extra symbols |
| `stocks+delisted`, `store+delisted` | Plus the delisted stocks |

## Delisted stocks (`delisted.py`)

The saved universe is today's listed stocks, so every past day is missing the stocks that were
later bought out, went bankrupt or were delisted (survivorship bias). `--include-delisted` adds
them from free Alpaca sources:

| Source | Gives | Coverage |
|---|---|---|
| Inactive assets (`/v2/assets?status=inactive`) on NYSE, NASDAQ, AMEX, minus fund names | Tickers, no dates | About 1,550 tickers |
| Corporate actions: the acquired side of mergers, and worthless removals | Tickers and dates | Sparse before 2020 |
| `--delisted-csv` (`symbol,last_day`) | Anything else, e.g. a paid list | Yours |

Each candidate's life is its daily bars with `asof="-"`, clipped at its last day when known.
Then:

- **Renames are dropped.** A ticker whose last bar equals a current symbol's bar that day is that
  company under its old name, and the current symbol already carries its history.
- **Reused tickers are split.** Days identical to the current symbol with the same ticker are
  dropped; the rest is stored as `TICKER@YYYYMMDD` (its last day). `StoreSource` never sends
  these keys to Alpaca, and `BarStore.request_symbol` maps them back for downloads.
- The daily bars are stored with the store's full window, so backtests treat them as complete;
  minute bars follow the normal plan.

Known gaps: delistings in neither source (for example the bank failures SIVB, FRC and SBNY), and
the early years (on 2026-11-02 the list's coverage before 2019 was thin). `delisted_info`
records the counts by year of each stock's last day.

## Tick store (`ticks.py`)

Trades and quotes for only the moments a backtest trades in, for tick fills
(`../backtest/README.md`, "Tick fills"). Default file: `trading-bot/data-store/ticks.sqlite`.

| Window | Contents |
|---|---|
| `minute_windows(symbol, minute)` | Every trade in the minute, and quotes from 10 s before it to its end |
| `moment_window(symbol, at)` | Quotes from 10 s before `at` to 5 s after (a market exit) |

- **Table `windows`:** one row per (symbol, kind, start, end), with a zlib-compressed JSON
  blob.
  - Trades are `[µs after start, price, size, conditions]`. Trades the tape canceled or
    replaced are dropped.
  - Quotes are `[µs after start, bid, ask, bid size, ask size]`.
  - An empty blob means "fetched, nothing there".
- **`StoreTicks`** implements the simulator's `TickSource` and counts missing windows in
  `misses`.
- **Fetching:** `BarFetcher.ticks(kind, symbols, start, end, asof)` pages `/v2/stocks/trades` or
  `/quotes` under the same rate limit and retries as bars. It drops ticks stamped exactly at
  `end`, because Alpaca's `end` is inclusive.


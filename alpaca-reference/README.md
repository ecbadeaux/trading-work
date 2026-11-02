# alpaca-reference

How to use the read-only Alpaca reference material in `thridparty-repos/`. **Paths here are
relative to the workspace root** (two levels up), because they point into `thridparty-repos/`.

**Up:** [`../README.md`](../README.md) · **Children:** [`alpaca-docs.md`](alpaca-docs.md)

## What's in thridparty-repos/

The folder name really is spelled `thridparty-repos`. **Claude never creates, edits, moves or
deletes anything in it.** The user updates it.

| Folder | What it is | How the user updates it |
|---|---|---|
| `thridparty-repos/alpaca-py/` | Clone of the official Alpaca Python SDK (`github.com/alpacahq/alpaca-py`) | `git -C thridparty-repos/alpaca-py pull` |
| `thridparty-repos/alpaca-docs/` | Offline markdown copy of https://docs.alpaca.markets/us/ (no upstream repo exists) | `trading-work/alpaca-reference/refresh-alpaca-docs.sh` |

Reading and searching these folders is fine and encouraged.

## How to answer Alpaca questions

1. **SDK usage** (class names, request objects, method signatures, return types): read the source
   in `thridparty-repos/alpaca-py/alpaca/`. It is ground truth and more reliable than memory.
2. **API behavior, rules and fields** (order types, margin, market hours, rate limits, data feeds,
   plan limits): grep `thridparty-repos/alpaca-docs/us/`. See [`alpaca-docs.md`](alpaca-docs.md)
   for which file is which.
3. **Working examples:** `thridparty-repos/alpaca-py/examples/` has notebooks for stocks, options
   and crypto, plus two full bot projects under `examples/stocks/`.

## alpaca-py SDK map

Install with `pip install alpaca-py` (Python ≥3.10, pydantic v2). Every package follows one
pattern:

- `client.py`: methods;
- `requests.py`: pydantic request objects passed into those methods;
- `models.py`: responses;
- `enums.py`.

All paths below are under `thridparty-repos/alpaca-py/`.

- **`alpaca/trading/`**
  - `TradingClient`: orders, positions, account, assets, watchlists, clock and calendar, options
    contracts.
  - `TradingStream` (`alpaca/trading/stream.py`): websocket for trade and order updates.
- **`alpaca/data/historical/`**
  - Clients: `StockHistoricalDataClient`, `CryptoHistoricalDataClient`,
    `OptionHistoricalDataClient`, `NewsClient`, `ScreenerClient`, `CorporateActionsClient`.
  - Request types are in `alpaca/data/requests.py`; `TimeFrame` is in
    `alpaca/data/timeframe.py`.
- **`alpaca/data/live/`:** websocket streams `StockDataStream`, `CryptoDataStream`,
  `OptionDataStream`, `NewsDataStream`.
- **`alpaca/broker/`:** `BrokerClient`, for running a brokerage for end users. Usually **not**
  needed for personal bots.
- **`alpaca/common/`:** shared REST base (pagination, and retries on 429/504), exceptions
  (`APIError`), enums such as `BaseURL`.

**Behavior worth knowing:**

- **Return types:** most client methods return `Model | RawData`.
- **Timestamps:** a naive datetime in a request is treated as UTC.
- **Calendar:** `Calendar.open`/`close` are naive US/Eastern times.
- **Streams:** `StockDataStream.run()` and `TradingStream.run()` call `asyncio.run()` and block
  the thread. To share an event loop, await `_run_forever()` instead (the SDK's own example
  notebooks do this).
- **Handlers:** stream handlers are awaited inline, so slow handlers stall the stream.
- **REST:** REST clients are synchronous (`requests`).

**Running the SDK's own tests** writes into the clone (a virtualenv and caches), so the user runs
them: `cd thridparty-repos/alpaca-py && poetry install && poetry run pytest`.

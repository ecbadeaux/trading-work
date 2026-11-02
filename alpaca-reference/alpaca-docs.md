# Offline Alpaca docs: layout and how to find pages

Guide to `thridparty-repos/alpaca-docs/`, an offline markdown copy of
https://docs.alpaca.markets/us/. **Paths here are relative to `thridparty-repos/alpaca-docs/`**
(at the workspace root). Read and grep it freely; never modify it.

**Up:** [`README.md`](README.md)

## Layout

- `us/docs/`: 108 guides.
- `us/reference/`: 264 endpoint pages, each with an embedded OpenAPI definition.
- `us/changelog/`: recent entries only.
- `llms-us.txt` (US) and `llms-all.txt` (all regions): indexes from the site. EU and India pages
  are listed in `llms-all.txt` but were not downloaded.
- `.last-refresh`: UTC time of the last refresh. Each page also has an `updatedAt` in its front
  matter.

## Finding the right page

- **`llms-us.txt` is the table of contents.** It groups every page under a section:
  Introduction, Broker API, Trading API, Market Data API, Connect API, FIX API, and an API
  Reference section for each API. Check it before trusting a file name.
- **File names are ambiguous.** The same topic exists separately for Broker API and Trading API,
  and the site adds `-1`/`-2` suffixes to keep slugs unique. For example:
  - `us/docs/crypto-trading-1.md` is Broker API; `us/docs/crypto-trading.md` is Trading API.
  - `us/reference/getallorders-1.md` is Trading API; `us/reference/getallordersforaccount.md` is
    Broker API.
- **For our bots, prefer the Trading API and Market Data API pages.**

Useful starting points, all in `us/docs/`:

| Topic | Pages |
|---|---|
| Getting started | `getting-started-with-trading-api.md`, `paper-trading.md` |
| Orders | `orders-at-alpaca.md` (types, time in force, bracket/OCO/OTO, extended hours) |
| Trading rules | `user-protection.md` (wash-trade rejections), `margin-and-short-selling.md`, `the-intraday-margin-rule.md` (replaced the pattern-day-trader rule) |
| Market data | `about-market-data-api.md` (plans, feeds), `market-data-faq.md`, `historical-api.md`, `streaming-market-data.md`, `real-time-stock-pricing-data.md`, `websocket-streaming.md` |
| Other assets | `options-trading.md`, `crypto-trading.md` |

Any live page is also available at `https://docs.alpaca.markets/us/docs/<slug>.md`. Fetching
a page from the web is fine; saving it into this folder is not.

## Refresh (user only)

`trading-work/alpaca-reference/refresh-alpaca-docs.sh` re-downloads `llms*.txt` and every US page
listed in it (6 in parallel, with retries) into this folder, then updates `.last-refresh`. The
user runs it; Claude doesn't, because it writes inside `thridparty-repos/`.

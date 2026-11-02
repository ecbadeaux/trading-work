# Configuration: API keys, env vars and endpoints

How the bot and the backtester get credentials and choose paper or live. Paths are relative to
`trading-bot/`.

**Up:** [`README.md`](README.md)

## Where to keep keys

- **Recommended:** `~/.config/trading-bot/.env`, outside any repo, with `chmod 600`. Pass it
  with `--env-file ~/.config/trading-bot/.env`. Start from the template `.env.example`.
- `trading-bot/.env` also works (it is loaded automatically and gitignored), but it lives inside
  the repo.
- **Don't** put keys in shell profiles (`~/.zshrc`): every process you start, including AI
  assistants' shells, inherits them.
- **Use paper keys** from the paper dashboard. Paper and live keys are different key pairs, and
  paper keys only work against the paper endpoint.
- If a key ever shows up in a log or chat, regenerate it in the Alpaca dashboard.

## How settings load

1. **Load:** `load_settings()` (`src/trading_bot/config.py`) reads environment variables.
   - It first loads `--env-file <path>` if given (the file must exist), otherwise `./.env` if
     present.
   - Variables already set in the process win over file values (`override=False`).
2. **Validate:**
   - `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` must be non-empty, or the program exits with
     code 2.
   - `ALPACA_PAPER` defaults to `true`. `ALPACA_PAPER=false` also requires
     `TRADING_BOT_ALLOW_LIVE=yes`, or the program exits with code 2.
3. **Connect:** `create_clients()` (`src/trading_bot/broker/clients.py`) is the only place that
   builds alpaca-py clients.
   - `TradingClient(key, secret, paper=settings.paper)`: the SDK picks paper →
     `https://paper-api.alpaca.markets` or live → `https://api.alpaca.markets`.
   - The data clients always use `https://data.alpaca.markets`, the same for paper and live.

`Settings.__repr__` masks both keys, so settings can be logged safely.

## Paper vs live: one switch only

**`ALPACA_PAPER` is the single source of truth.** There is deliberately no base-URL setting:

- **Don't add `APCA_API_BASE_URL`.** It is a holdover from the old `alpaca-trade-api` SDK that
  many tutorials still show, and alpaca-py ignores it.
- **Don't pass `url_override` to any client.** In alpaca-py, `url_override` takes precedence over
  `paper`, so a URL setting could send real orders while the bot reports PAPER and passes the live
  guard.
- **If an override is ever needed** (a local mock or proxy), add an explicit
  `TRADING_BOT_URL_OVERRIDE` that startup rejects when it points at `api.alpaca.markets` while
  `ALPACA_PAPER=true`.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `APCA_API_KEY_ID` | yes | none | Alpaca API key ID |
| `APCA_API_SECRET_KEY` | yes | none | Alpaca API secret |
| `ALPACA_PAPER` | no | `true` | Paper vs live endpoint |
| `TRADING_BOT_ALLOW_LIVE` | only if live | `no` | Second confirmation for live trading |
| `TRADING_BOT_DRY_RUN` | no | `false` | Never submit orders (same as `--dry-run`) |
| `TRADING_BOT_LOOP_INTERVAL` | no | `60` | Seconds between bot ticks |
| `TRADING_BOT_LOG_LEVEL` | no | `INFO` | Log level (same as `--log-level`) |
| `TRADING_BOT_LOG_DIR` | no | `logs` | Log directory (rotating `trading-bot.log`) |

The backtester reads the same keys (data access only) and takes everything else as command-line
flags. See [`../src/trading_bot/backtest/README.md`](../src/trading_bot/backtest/README.md).

## Market data plan

| | Free Basic plan | Algo Trader Plus |
|---|---|---|
| Real-time feed | IEX only (about 2.5% of volume) | SIP |
| Streaming | 30 symbols | Unlimited |
| Rate limit | 200 calls/min | 10,000 calls/min |
| SIP history | All but the last 15 minutes | All |

- **Backtesting works on the free plan.**
- **Live ORB needs SIP**, because relative volume has to cover the whole market at 09:35.
- Most accounts get one market-data stream connection.

The details are in `thridparty-repos/alpaca-docs/us/docs/about-market-data-api.md`, relative to
the workspace root.

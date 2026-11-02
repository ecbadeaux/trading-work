# docs

Design documents for the bot. Paths are relative to `trading-bot/`.

**Up:** [`../README.md`](../README.md)

| Doc | What it covers |
|---|---|
| [`configuration.md`](configuration.md) | API keys, env vars, how settings load, why `ALPACA_PAPER` is the only paper/live switch, data plans |
| [`research/orb.md`](research/orb.md) | Opening range breakout: research consensus, what others found, sources, the algorithm we implement, Alpaca mapping, how results are judged |
| [`research/results.md`](research/results.md) | **Every test we ran**: a register (what, data, result, verdict, why) and the details of each |
| [`research/walkforward-orb-v1.toml`](research/walkforward-orb-v1.toml) | The pre-registered walk-forward protocol for ORB: grid, years, selection rule, pass criteria (run with `trading-bot-walkforward`) |

## Conventions

- **One research doc per strategy** in `research/`. Each covers the evidence, the rules
  implemented, how they map to Alpaca, known limits and sources.
- **Record every test in `research/results.md`:** a register row (verdict and why) and the
  details (date, universe, period, settings). Label small samples as smoke tests.
- **Judge strategies with a pre-registered walk-forward spec**, written before its results are
  seen. A changed spec is a new version; the old runs keep counting as trials in the ledger.
- **When code changes a documented rule or default, update the doc in the same change.**

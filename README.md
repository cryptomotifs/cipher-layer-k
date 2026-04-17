# cipher-layer-k

**Layer K** — the autonomous Solana trading scaffold that sits downstream of the
[CIPHER signal engine](https://github.com/cryptomotifs/cipher-signal-engine).

Layer K takes a ranked signal and walks it through:

`SafetyGate → OracleValidator → JupiterQuote → SlippageGuard → TxBuilder → TxSigner (isolated) → JitoClient / RPC → FillMonitor → PnL`

This repo ships the 7 **P0** modules from the architecture brief in a
**paper-trade-only** configuration. **No live mainnet execution is enabled.**
See [Why paper-trade only](#why-paper-trade-only) below.

Related projects:

- [`cipher-signal-engine`](https://github.com/cryptomotifs/cipher-signal-engine) —
  the signal producer (stocks + crypto, daily/weekly). Layer K is its trading
  sidecar.
- [cipher-x402.vercel.app](https://cipher-x402.vercel.app) — public marketing +
  pricing site.

---

## Why paper-trade only

CIPHER must prove profitability on its own capital **before** it sells signals
to subscribers. That's a rule. Today the agent wallet
(`BAuuhx7eZMPnN3vH7R2VU1GYZtVZmmYWCUgEetiF2HQv`) is unfunded, so this scaffold:

- **Refuses to broadcast real transactions.** `PAPER=1` is the default; live
  mode is gated behind an explicit env flag plus a funded-wallet balance check.
- **Still executes every read path for real** — Jupiter quotes, Jito bundle
  construction, on-chain blockhash fetch, Pyth oracle reads. We want to shake
  out integration bugs _before_ any lamports move.
- **Writes a full trade journal to SQLite** so PnL attribution works
  immediately in paper mode. When the wallet is funded, the same journal
  continues to tick over in live mode with zero code change.

Current status: scaffold-only. First funded trade will be a whitelisted
SOL → USDC → SOL round trip with a `$10` cap.

---

## The 7 P0 modules

| # | Module | What it does |
|---|---|---|
| 1 | `wallet.py` | Loads a hot keypair from the AES-256-GCM encrypted keystore under `~/cipher-secrets/`. Exposes `pubkey()` + `sign()`. Daily-outflow ledger in SQLite blocks trades past a configurable lamport cap. |
| 2 | `jupiter_client.py` | Thin sync wrapper over Jupiter Lite (`https://lite-api.jup.ag/`). `get_quote()` + `get_swap_transaction()`. No API key needed. |
| 3 | `tx_signer.py` | Spawns a subprocess that holds the keypair. Parent sends a transaction + program-ID allowlist over a pipe. Child validates every instruction touches only allowlisted programs (Jupiter v6, Token-2022, Compute Budget, SPL Token) and refuses otherwise. |
| 4 | `jito_client.py` | Python port of the proven `sol-volume-bot-v3` Jito pattern. Picks a tip account at random, builds a `sendBundle` body, broadcasts to the 5 regional Jito endpoints. **Dry-run mode by default** — logs the bundle instead of POSTing. |
| 5 | `executor.py` | Orchestrator. Takes a signal + size → Jupiter quote → isolated signer → Jito/RPC submit. In `PAPER=1` mode every trade writes to SQLite with a synthetic fill. Returns a `TradeResult`. |
| 6 | `emergency_halt.py` | 5 trip conditions: daily cap breach, 3-loss streak > 5%, oracle disagreement > 50 bps, 3 consecutive Jupiter failures, manual `~/cipher-secrets/HALT` flag file. When tripped, executor refuses all new trades. |
| 7 | `pnl_tracker.py` | Reads the trade log, computes running PnL, rolling 30-day Sharpe, max drawdown, and win-rate. `.summary()` for daily ops logs, `.to_csv()` for public posting. |

Each module ships with a unit test under `tests/` that mocks HTTP and
subprocess IO. `pytest` + `ruff check` pass in CI on every push.

---

## Quickstart

```bash
git clone https://github.com/cryptomotifs/cipher-layer-k.git
cd cipher-layer-k
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"

# Run the tests
pytest -q
ruff check .

# Paper-trade smoke (no network — synthesises the quote)
PAPER=1 python -m cipher_layer_k.executor --signal-demo
```

See [`docs/paper-trade-runbook.md`](docs/paper-trade-runbook.md) for the full
runbook (keystore creation, daily-cap config, halt-flag usage, reading the
SQLite trade log).

---

## Security posture

- Private keys **never** hit disk in plaintext and **never** enter an env var.
- Only the isolated `tx_signer` subprocess ever holds the decrypted keypair in
  memory; the parent process only sees signatures coming back.
- Program-ID allowlist is hardcoded in `tx_signer.py` — any instruction
  targeting a program not in the list is rejected at signing time. This
  defeats the "quote returns an instruction that drains your wallet" class of
  attack even if Jupiter's API were compromised.
- Daily outflow cap tracks **cumulative lamports out** (not count of trades)
  in a local SQLite ledger. Cap enforced on every `executor.execute()` call.
- The `EmergencyHalt` flag file (`~/cipher-secrets/HALT`) is a 1-second kill
  switch — create the file, the next intent fails fast.

---

## What's NOT here (yet)

- Real mainnet submission path (gated behind `LIVE=1` + funded-wallet check).
- Position manager & stop-loss monitor — Phase 2 (Day 5 of the 7-day plan).
- On-chain fill parsing beyond the trade journal — the `fill_monitor` in
  the signal-engine repo handles this today.
- Integration with `cipher/trading/bus.py` — the signal bus lives in the
  signal-engine repo; this scaffold consumes a `RankedSignal`-shaped dict
  directly.

---

## License

MIT — see [LICENSE](LICENSE).

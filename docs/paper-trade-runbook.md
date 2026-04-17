# Paper-trade runbook

This document walks an operator through enabling paper-trading mode,
watching the trade log, and (when the wallet is funded) flipping to
live mode for a single whitelisted trade.

---

## 0. Prerequisites

- Python 3.11+
- `pip install -e ".[dev]"` inside a clean virtualenv
- An encrypted keystore at `~/cipher-secrets/agent-wallet-keystore.json`
  (see the signal-engine repo's `secrets/agent-wallet-keystore.json` for
  the canonical AES-256-GCM + HKDF-SHA256 schema)
- The keystore passphrase set in env:
  ```bash
  export CIPHER_WALLET_PASSPHRASE='your-passphrase'
  ```

The scaffold is useful **even without a keystore** — the executor runs
fully in paper mode without touching the wallet.

---

## 1. Paper-mode smoke test

```bash
# PAPER=1 is the default — LIVE=1 must be set explicitly to flip.
PAPER=1 python -m cipher_layer_k.executor --signal-demo
```

Expected output (last line):

```
executor.trade id=<uuid> status=paper_filled mode=paper
```

The trade is logged to `~/.cipher-layer-k/trades.db`. Inspect it:

```python
from cipher_layer_k.pnl_tracker import PnLTracker
print(PnLTracker().summary())
```

---

## 2. Reading the trade journal

```bash
python -c "from cipher_layer_k.pnl_tracker import PnLTracker; print(PnLTracker().to_csv())"
```

Columns: `trade_id, created_at_utc, mode, side, filled_size_usd,
realised_pnl_usd, status`. Status values:

- `paper_filled` — synthetic fill from Jupiter quote (no network write)
- `landed` — Jito bundle landed on-chain (live mode)
- `failed` — Jupiter, signer, or Jito failure (see `error` column)
- `rejected` — blocked by emergency halt

---

## 3. The kill switch

Any one of these trips the halt; all new trades will return `rejected`:

1. **Daily lamport cap** — defaults to 0.05 SOL. Configurable via the
   `daily_cap_lamports` kwarg on `EmergencyHalt(...)`.
2. **Loss streak** — 3 consecutive losing trades totalling > 5%.
3. **Oracle divergence** — any recorded divergence > 50 bps (see
   `EmergencyHalt.record_oracle_divergence(bps)`).
4. **Jupiter failure streak** — 3 consecutive quote errors.
5. **Manual flag** — `touch ~/cipher-secrets/HALT`. Delete the file **and**
   call `EmergencyHalt().clear()` to recover.

To force a halt right now:

```bash
mkdir -p ~/cipher-secrets && touch ~/cipher-secrets/HALT
```

The next `executor.execute()` call returns:

```
TradeResult(status='rejected', error='halt_tripped: trading halted: manual flag file present', ...)
```

---

## 4. Live mode (not yet, but here's how)

The wallet (`BAuuhx7eZMPnN3vH7R2VU1GYZtVZmmYWCUgEetiF2HQv`) is unfunded
today. When it's funded with at least 0.05 SOL:

```bash
export LIVE=1                              # override paper mode
export CIPHER_WALLET_PASSPHRASE='...'      # needed for signer subprocess
python -m cipher_layer_k.executor --signal-demo  # (update demo first)
```

The full pipeline then runs:
`Jupiter quote → Jupiter swap-tx → IsolatedSigner (program-id allowlist)
→ Jito bundle → landed-or-failed`.

**Before** flipping `LIVE=1` for the first time, sanity-check:

```python
from cipher_layer_k.emergency_halt import EmergencyHalt
from cipher_layer_k.wallet import OutflowLedger
halt = EmergencyHalt(ledger=OutflowLedger())
print(halt.state.as_dict())  # every field 0 / False
```

---

## 5. What to do if a trade lands wrong

- Set the manual halt: `touch ~/cipher-secrets/HALT`.
- Inspect `~/.cipher-layer-k/trades.db` (`sqlite3`), find the bad row,
  grab the `tx_signature`.
- Trace the tx on Solscan / Solana Beach.
- Fix, test, remove the halt flag, run `EmergencyHalt().clear()` and
  resume.

The daily outflow ledger (`~/.cipher-layer-k/outflow.db`) is append-only
by design — do **not** edit it to "reset" the cap. Wait for UTC midnight
or raise the cap in config (and record why).

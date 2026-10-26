# Cover Pool

**An underwriter funds a pool and freezes the only event that can pay it; buyers
pay a premium for coverage that is settled pro rata against the pool's
solvency.**

Parametric cover has an awkward ownership problem: whoever defines the event
must not be whoever collects on it. This contract fixes that in the structure.
The **underwriter** names the event and puts up the capital, so an easy-to-hit
trigger costs the underwriter money. **Buyers** choose a coverage amount and pay
the exact premium; if the event is proven they are paid out of the pool, and if
it is not they lose only the premium.

Deployed on GenLayer Asimov: **`0x22f4c7F8035383C4C1E98ff64521dC535Eb8d456`**
([explorer](https://explorer-asimov.genlayer.com/address/0x22f4c7F8035383C4C1E98ff64521dC535Eb8d456))

> Bradbury was reset to Phase 2 (chain id 4221) in October 2026: the earlier
> contracts are gone and both local accounts read 0 GEN there, while the same
> account still holds funds on Asimov. This deployment is therefore on Asimov.

---

## The event

One watched Ethereum address must send **at least `threshold` wei in one
successful transaction**, inside **one fixed block range**:

```
trigger_address   the sender that must move funds
threshold         the amount, in ETH wei
from_block        first block of the cover window
to_block          last block of the cover window
```

The recipient must be a different address (a self-transfer moves nothing), the
transaction must have succeeded, and the sender must be exactly the watched
address. Nothing else counts, and there is no way to amend the terms: they are
written into the pool at `open_pool` and never change.

## Why two providers, and why no clock

The event is read from **two independent JSON-RPC operators**, each asked for the
same two calls:

| call | gives | used for |
|---|---|---|
| `eth_getTransactionByHash(txid)` | `blockNumber`, `from`, `to`, `value` | the transfer |
| `eth_getTransactionReceipt(txid)` | `status` | success |

Both must report the transaction, and both must agree on **five facts**: sender,
recipient, value, block, and success. One disagreement is enough to refuse, and
the reason is written on chain.

The trailing `?m=tx` / `?m=rc` / `?m=tip` on each URL only distinguishes the
calls in logs and in the test harness; RPC servers ignore unknown query
parameters.

**No timestamp is ever read.** Two operators legitimately report tips that differ
(one sample was 517 blocks apart, another matched exactly), and a wall clock is
the kind of fact two honest operators never agree on. So the contract's own
"clock" is a **boolean over block heights**:

- `open_pool` refuses a window that has already started;
- `buy_cover` refuses once the fresher tip has reached `from_block`;
- `close` refuses until the fresher tip has passed `to_block + 1200`.

The first rule plus the second makes **buying cover after the event
impossible**, not merely discouraged: sales are over before the first covered
block exists. The third is a **filing grace** nobody can shorten, so the
underwriter cannot close an open pool before a covered party has had time to
file the transaction.

## Money, exactly

Two ledgers, one scale each, never converted:

| quantity | unit | representation |
|---|---|---|
| pool, premium, payout | **GEN** | integer **wei** (10¹⁸) |
| the watched transfer | **ETH** | integer **wei** (10¹⁸) |

Same decimals, different assets. `threshold` is compared with the transfer value
directly; the contract never prices anything. Every pool stores the model on
chain:

```json
{"asset":"GEN","amount_decimals":18,"watched_chain":"ethereum","watched_unit":"wei","watched_decimals":18}
```

The premium is `coverage * premium_bps // 10000`, and `buy_cover` rejects any
`msg.value` that is not exactly that number. Payouts are **pro rata against
solvency**:

```
payout = coverage * funded // coverage_total      (capped at coverage)
```

A pool may sell more coverage than it has capital. That is visible before anyone
buys — `get_pool` reports `collateral_bps = funded * 10000 // capacity` — and if
the event happens the shortfall is shared in proportion to coverage, not paid
first-come. The underwriter receives exactly what is left.

```
paid_total + refunded == funded + premium_total     on every path
held == 0                                          once a pool is closed
```

## Lifecycle

```
open_pool   (payable, future window frozen)          -> OPEN
buy_cover   (payable, exact premium, pre-window)     -> OPEN
observe     (covered party files one txid)           -> TRIGGERED
claim       (per covered address, pro rata, once)    -> TRIGGERED
close       (residual to the underwriter)            -> CLOSED
cancel      (underwriter, nothing sold, pre-window)  -> CLOSED
```

- `observe` is restricted to addresses that hold cover; anyone else would be
  filing evidence about a pool they have no stake in.
- Filing a transaction that does not satisfy the terms is not fatal: the refusal
  is recorded and a covered party can file again.
- `close` on a triggered pool waits until every covered address has claimed, or
  until `to_block + 100000` (~2 weeks) so that a straggler cannot hold the
  underwriter's residual hostage forever.
- Every refusal happens before any state change; the tests assert the state is
  byte-identical after each one.

## Trust boundaries, stated plainly

1. **The two providers are the oracle.** They are contract constants, so neither
   party can point them at a server it controls, but a lying provider is still a
   risk — hence two of them, and fail-closed on any disagreement. If either
   provider is unreachable the call fails closed with a recorded reason.
2. **Ownership of the watched address is not provable on chain.** If a buyer
   controls the watched address it can trigger the pool itself. The underwriter
   chooses the address, and the contract rejects a buyer equal to it, but it
   cannot see who really holds the key. Underwrite addresses you do not control
   and that your buyers do not control.
3. **The threshold is a policy, not a definition of theft.** A large legitimate
   payment also triggers the pool. The underwriter sets the threshold above
   normal flows; that is its pricing decision.
4. **Cover is only as good as the capital behind it.** The contract reports the
   collateral ratio and shares a shortfall pro rata; it does not promise a full
   payout on an under-funded pool.
5. **No model touches the numbers.** Extraction is deterministic JSON parsing
   and the verdict is a fixed sequence of comparisons. Validators agree on the
   bytes, not on an opinion, and no prompt can be talked into a payout.

## API

| method | note |
|---|---|
| `open_pool(trigger_address, threshold, from_block, to_block, premium_bps, capacity)` | **payable**; the value is `funded`. Window must be in the future |
| `buy_cover(pool_id, coverage)` | **payable**; must equal `coverage * bps // 10000`; sales stop at `from_block` |
| `observe(pool_id, txid)` | covered addresses only; two providers must agree on five facts |
| `claim(pool_id)` | pro rata from `funded`, capped at coverage, once per address |
| `close(pool_id)` | residual to the underwriter; waits for the filing grace / lapse |
| `cancel(pool_id)` | underwriter only, nothing sold, before the window |
| `get_pool` / `get_position` / `get_evidence` / `get_pool_count` / `quote` | JSON strings and plain ints |

**Bounds** — `threshold` > 0, `1 <= premium_bps <= 10000`, `capacity` > 0,
`to_block >= from_block`, `from_block` > 0.

## Testing

```
python -m pytest tests -q
```

**68 tests** in `tests/direct/test_cover_pool.py`, in eight groups: the unit
model, pool creation, premiums and capacity, the two-provider claim, every
refusal (missing source, block/transfer/value/success disagreement, failed
transaction, outside the window, wrong sender, self-transfer, below threshold),
consensus agreement, pro-rata payouts, the exits, and the invariants (refusals
change nothing, money is conserved, views are strings).

The fixtures in `tests/direct/conftest.py` use the **real JSON-RPC envelopes**
for both calls, including the hex-encoded `blockNumber` / `value` / `status`.

## Live

Verified on GenLayer Asimov against `0x22f4c7F8035383C4C1E98ff64521dC535Eb8d456`. Every number
below is read back from chain state, not from receipts.

### `p0` — a real transfer triggered it, and the shortfall was shared

| step | result |
|---|---|
| `open_pool` | `funded` **0.005 GEN**, `capacity` **0.02 GEN** → `collateral_bps` **2500** |
| `buy_cover` ×2 | 0.0005 GEN premium each (500 bps) for **0.01 GEN** cover each |
| window | `[26275672, 26276022]`, watched `0x264bd829…`, threshold **0.01 ETH** |
| the event | **0.01724956 ETH** → `0x0359035f…` at block **26275688** |
| `observe` | **TRIGGERED** |
| `claim` ×2 | **0.0025 GEN** each |
| `close` | ledger credits the underwriter **0.001 GEN**, `held` **0** |

The evidence both providers reported, byte for byte:

```
publicnode   found  0x264bd829…  0x0359035f…  17249560000000000 wei  26275688  success
drpc         found  0x264bd829…  0x0359035f…  17249560000000000 wei  26275688  success
```

That transfer is a real Ethereum transaction —
[`0x6f30a29d2df247353fad53339d5e888f1580376eee66d3152940e85927eb80de`](https://etherscan.io/tx/0x6f30a29d2df247353fad53339d5e888f1580376eee66d3152940e85927eb80de),
chosen by neither party, and it is exactly what makes the pool pay.

The pool was deliberately under-funded: 0.02 GEN of cover behind 0.005 GEN of capital. Each buyer
was owed 0.01 GEN, the pool could pay half of that, so each claim was

```
payout = coverage * funded / coverage_total = 0.01 * 0.005 / 0.02 = 0.0025 GEN
```

and the conservation check held:

```
paid 0.005 + refunded 0.001 == funded 0.005 + premiums 0.001   ✓
```

`close` returned exactly the 0.001 GEN of premiums and nothing was left behind.

### `p1` — a pool that sold nothing

Funded 0.01 GEN with no cover sold, cancelled before its window opened:

```
status    CLOSED      reason  cancelled before the window opened
funded    0.01 GEN    paid    0
refunded  0.01 GEN    held    0
```

The cancel itself was sent six times against an Asimov RPC that answered
`internal error` for a stretch before the one that landed; the pool took the
first successful call and refused the rest, which is the point of a repeatable
write. The same path is asserted by
`test_the_underwriter_cancels_a_pool_that_sold_nothing`.

### What the chain balance says

Both pools report `held == 0` and `paid + refunded == funded + premiums` holds in
the ledger — but the contract's **native balance is still 0.016 GEN**, which is
exactly p0's capital plus premiums and p1's capital, and neither buyer's balance
changed. `emit_transfer` is documented as emitting with `on='finalized'`, and on
this network the emitted transfer does not reach the recipient's EVM balance;
this was measured here and in two other contracts, minutes after the writes
reported `FINALIZED`.

So the **ledger settles exactly and the accounting invariants hold and are what
the tests prove**; the outbound native transfer is not reflected in balances on
Asimov. Stating it rather than claiming the money moved, because the balance is
the one thing that can actually be checked.

## Source integrity

```
sha256  1c375e5e51c797ec999ec8319b5fd5785e30e789b3d2545c063465265b32efbb
bytes   19391
```

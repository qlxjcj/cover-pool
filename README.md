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

Deployed on GenLayer Bradbury: **(filled after the deploy)**

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

## Why two indexers, and why no clock

The event is read from **two independent indexers**:

| indexer | endpoint | shape |
|---|---|---|
| blockscout | `eth.blockscout.com/api/v2/transactions/<txid>` | `block_number`, `from.hash`, `to.hash`, `value` (decimal), `result` |
| routescan | `api.routescan.io/.../etherscan/api?module=proxy&action=eth_getTransactionByHash` | `blockNumber`, `from`, `to`, `value` (hex) |

Both must report the transaction, and both must agree on **five facts**: sender,
recipient, value, block, and success. One disagreement is enough to refuse, and
the reason is written on chain.

**No timestamp is ever read.** In the live data below the two indexers' tips were
517 blocks apart (about 1.7 hours), and a wall clock is exactly the kind of fact
two honest operators never agree on. So the contract's own "clock" is a
**boolean over block heights**:

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
contract balance == 0                               after the last pool closes
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

1. **The indexers are the oracle.** They are contract constants, so neither
   party can point them at a server it controls, but a dishonest indexer is still
   a single point of failure — hence two of them, and fail-closed on any
   disagreement.
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
| `observe(pool_id, txid)` | covered addresses only; two indexers must agree on five facts |
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
model, pool creation, premiums and capacity, the two-indexer claim, every
refusal (missing source, block/transfer/value/success disagreement, failed
transaction, outside the window, wrong sender, self-transfer, below threshold),
consensus agreement, pro-rata payouts, the exits, and the invariants (refusals
change nothing, money is conserved, views are strings).

The fixtures in `tests/direct/conftest.py` use the **real payload shapes** of
both indexers, including the hex-encoded routescan proxy envelope and the
decimal-string blockscout envelope.

## Live

**(filled after the deploy)**

## Source integrity

```
sha256  (filled after the deploy)
bytes   (filled after the deploy)
```

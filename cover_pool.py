# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
import json
from genlayer import *

# GEN escrow on GenLayer in wei (10**18); the watched ETH transfer in wei
# (10**18). The threshold is compared with the transfer value directly.
#
# No clock: every time signal is a block height from two RPC providers, and
# each guard is a boolean - their tips can differ by a few blocks.

GEN_DECIMALS = 18
ETH_DECIMALS = 18
OPEN = "OPEN"
TRIGGERED = "TRIGGERED"
CLOSED = "CLOSED"
LAPSE_BLOCKS = 100000
# A covered party needs time to notice the transfer and file it, so an open
# pool cannot be closed until this many blocks after its window ends.
FILING_BLOCKS = 1200

# Two independent JSON-RPC operators. The trailing marker only distinguishes
# the two calls per operator; RPC servers ignore unknown query parameters.
RPC_A = "https://ethereum-rpc.publicnode.com/?m="
RPC_B = "https://1rpc.io/eth?m="
HDR = {"content-type": "application/json"}


class CoverPool(gl.Contract):
    """Solvency-capped cover for one on-chain event.

    The underwriter funds it and freezes the only event that can pay: one
    watched address sending at least `threshold` wei in one successful
    transaction inside one block range. Buyers pay a premium and are paid pro
    rata if it happens; nobody votes, and two providers must agree."""

    pools: TreeMap[str, str]
    positions: TreeMap[str, str]
    evidence: TreeMap[str, str]
    pool_count: u256

    def __init__(self):
        self.pool_count = 0

    def _addr_hex(self, a) -> str:
        if isinstance(a, str):
            return a
        h = getattr(a, "as_hex", None)
        if isinstance(h, str):
            return h
        if isinstance(a, (bytes, bytearray)):
            return "0x" + bytes(a).hex()
        return str(a)

    def _is_evm(self, a) -> bool:
        if not isinstance(a, str) or not a.startswith("0x") or len(a) != 42:
            return False
        try:
            int(a[2:], 16)
        except ValueError:
            return False
        return True

    def _body(self, content) -> str:
        b = getattr(content, "body", None)
        if b is None:
            return str(content)
        return b.decode("utf-8", errors="replace") if isinstance(b, bytes) else str(b)

    def _transfer(self, to: Address, amount: int):
        if amount > 0:
            gl.get_contract_at(to).emit_transfer(value=u256(amount))

    def _load(self, pid: str) -> dict:
        raw = self.pools.get(str(pid), "")
        if not raw:
            raise gl.vm.UserError("Pool not found")
        return json.loads(raw)

    def _save(self, d: dict):
        self.pools[d["pool_id"]] = json.dumps(d)

    def _pos(self, pid: str, who: str) -> dict:
        raw = self.positions.get(str(pid) + "|" + str(who).lower(), "")
        if not raw:
            return {"holder": str(who).lower(), "coverage": 0, "premium": 0,
                    "claimed": False, "paid": 0}
        return json.loads(raw)

    def _put_pos(self, pid: str, p: dict):
        self.positions[str(pid) + "|" + p["holder"]] = json.dumps(p)

    def _held(self, d: dict) -> int:
        return int(d["funded"]) + int(d["premium_total"]) - int(d["paid_total"])

    def _txid(self, text) -> str:
        s = str(text).strip().lower()
        if len(s) != 64:
            return ""
        for ch in s:
            if ch not in "0123456789abcdef":
                return ""
        return s

    def _hex_int(self, body: str) -> int:
        try:
            data = json.loads(body)
        except Exception:
            return 0
        if not isinstance(data, dict):
            return 0
        v = data.get("result")
        if isinstance(v, int):
            return v
        if not isinstance(v, str):
            return 0
        s = v.strip().lower()
        if not s.startswith("0x") or len(s) < 3:
            return 0
        try:
            return int(s[2:], 16)
        except ValueError:
            return 0

    # ---------------- the block clock, as a boolean ----------------

    def _tip(self) -> int:
        """The fresher of the two tips, or 0 when either is unreadable."""
        payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                              "method": "eth_blockNumber", "params": []})

        def run() -> int:
            a = 0
            b = 0
            try:
                a = self._hex_int(self._body(gl.nondet.web.post(
                    RPC_A + "tip", body=payload, headers=HDR)))
            except Exception:
                a = 0
            try:
                b = self._hex_int(self._body(gl.nondet.web.post(
                    RPC_B + "tip", body=payload, headers=HDR)))
            except Exception:
                b = 0
            if a <= 0 or b <= 0:
                return 0
            return a if a > b else b

        def agrees(leader) -> bool:
            return isinstance(leader, gl.vm.Return) and int(leader.calldata) == run()

        return int(gl.vm.run_nondet_unsafe(run, agrees))

    def _tip_ok(self, block: int, before: bool) -> bool:
        t = self._tip()
        if t <= 0:
            return False
        return t < int(block) if before else t > int(block)

    # ---------------- one transaction, as two providers saw it ----------------

    def _rpc_tx(self, body_tx: str, body_rc: str, txid: str) -> dict:
        out = {"found": False, "from": "", "to": "", "value": 0, "block": 0, "ok": False}
        try:
            tx = json.loads(body_tx)
            rc = json.loads(body_rc)
        except Exception:
            return out
        if not isinstance(tx, dict) or not isinstance(rc, dict):
            return out
        tx = tx.get("result")
        rc = rc.get("result")
        if not isinstance(tx, dict) or not isinstance(rc, dict):
            return out
        if str(tx.get("hash", "")).lower() != txid:
            return out
        if str(rc.get("transactionHash", "")).lower() != txid:
            return out
        blk = tx.get("blockNumber")
        if not isinstance(blk, str) or not blk.startswith("0x"):
            return out
        try:
            height = int(blk, 16)
        except ValueError:
            return out
        if height <= 0:
            return out
        val = tx.get("value")
        try:
            amount = int(val, 16) if isinstance(val, str) and val.startswith("0x") else 0
        except ValueError:
            return out
        out["found"] = True
        out["from"] = str(tx.get("from") or "").lower()
        out["to"] = str(tx.get("to") or "").lower()
        out["value"] = amount
        out["block"] = height
        out["ok"] = str(rc.get("status", "")).lower() == "0x1"
        return out

    def _verdict(self, a: dict, b: dict, d: dict) -> str:
        # Fixed order: the reason is part of what validators agree on.
        if not a["found"]:
            return "the first provider has no such transaction"
        if not b["found"]:
            return "the second provider has no such transaction"
        if a["block"] != b["block"]:
            return "the providers disagree on the block"
        if a["from"] != b["from"] or a["to"] != b["to"]:
            return "the providers disagree on the transfer"
        if a["value"] != b["value"]:
            return "the providers disagree on the value"
        if a["ok"] != b["ok"]:
            return "the providers disagree on success"
        if not a["ok"]:
            return "the transaction failed"
        if a["block"] < int(d["from_block"]):
            return "the transfer is before the window"
        if a["block"] > int(d["to_block"]):
            return "the transfer is after the window"
        if a["from"] != str(d["trigger_address"]).lower():
            return "the sender is not the covered address"
        if not a["to"] or a["to"] == a["from"]:
            return "the transfer does not send funds away"
        if a["value"] < int(d["threshold"]):
            return "the transfer is below the threshold"
        return ""

    def _agree(self, his, mine) -> bool:
        if not isinstance(his, dict) or not isinstance(mine, dict):
            return False
        for k in ("a", "b", "matched", "reason"):
            if str(his.get(k)) != str(mine.get(k)):
                return False
        return True

    # ---------------- lifecycle ----------------

    @gl.public.write.payable
    def open_pool(self, trigger_address: str, threshold: int, from_block: int,
                  to_block: int, premium_bps: int, capacity: int) -> str:
        """Fund a pool and freeze the one event that can ever pay it."""
        under = self._addr_hex(gl.message.sender_address)
        funded = int(gl.message.value)
        if funded <= 0:
            raise gl.vm.UserError("Fund the pool with GEN")
        if not self._is_evm(trigger_address):
            raise gl.vm.UserError("Covered address must be 0x")
        try:
            thr = int(threshold)
            lo = int(from_block)
            hi = int(to_block)
            bps = int(premium_bps)
            cap = int(capacity)
        except Exception:
            raise gl.vm.UserError("Integers only")
        if thr <= 0:
            raise gl.vm.UserError("Threshold must be > 0 wei")
        if lo <= 0 or hi < lo:
            raise gl.vm.UserError("Need a block range")
        if not 1 <= bps <= 10000:
            raise gl.vm.UserError("Premium must be 1..10000 bps")
        if cap <= 0:
            raise gl.vm.UserError("Capacity must be > 0")
        # A pool opens only on a window that has not started: this is what
        # makes "buy cover after the event" impossible by construction.
        if not self._tip_ok(lo, True):
            raise gl.vm.UserError("The window has already started")
        pid = "p" + str(int(self.pool_count))
        self.pools[pid] = json.dumps({
            "pool_id": pid, "underwriter": under,
            "trigger_address": self._addr_hex(trigger_address).lower(),
            "threshold": thr, "from_block": lo, "to_block": hi,
            "premium_bps": bps, "capacity": cap, "funded": funded,
            "status": OPEN, "coverage_total": 0, "premium_total": 0,
            "paid_total": 0, "claimed_coverage": 0, "refunded": 0,
            "event_txid": "", "reason": "",
            "units": {"asset": "GEN", "amount_decimals": GEN_DECIMALS,
                      "watched_chain": "ethereum", "watched_unit": "wei",
                      "watched_decimals": ETH_DECIMALS},
        })
        self.pool_count = u256(int(self.pool_count) + 1)
        return pid

    @gl.public.write.payable
    def buy_cover(self, pool_id: str, coverage: int) -> str:
        """Pay exactly the quoted premium; sales stop when the window starts."""
        d = self._load(pool_id)
        if str(d["status"]) != OPEN:
            raise gl.vm.UserError("Pool is " + str(d["status"]))
        low = self._addr_hex(gl.message.sender_address).lower()
        if low == str(d["underwriter"]).lower():
            raise gl.vm.UserError("The underwriter cannot buy")
        if low == str(d["trigger_address"]).lower():
            raise gl.vm.UserError("The covered address cannot buy")
        try:
            cov = int(coverage)
        except Exception:
            raise gl.vm.UserError("Coverage must be an integer")
        if cov <= 0:
            raise gl.vm.UserError("Coverage must be > 0")
        prem = cov * int(d["premium_bps"]) // 10000
        if prem <= 0:
            raise gl.vm.UserError("Premium rounds to zero")
        if int(gl.message.value) != prem:
            raise gl.vm.UserError("Send exactly " + str(prem) + " wei")
        if int(d["coverage_total"]) + cov > int(d["capacity"]):
            raise gl.vm.UserError("Capacity exceeded")
        if not self._tip_ok(int(d["from_block"]), True):
            raise gl.vm.UserError("The window has already started")
        p = self._pos(pool_id, low)
        p["coverage"] = int(p["coverage"]) + cov
        p["premium"] = int(p["premium"]) + prem
        self._put_pos(pool_id, p)
        d["coverage_total"] = int(d["coverage_total"]) + cov
        d["premium_total"] = int(d["premium_total"]) + prem
        self._save(d)
        return "COVERED"

    @gl.public.write
    def observe(self, pool_id: str, txid: str) -> str:
        """A covered party files one txid; both providers must agree on it."""
        d = self._load(pool_id)
        if str(d["status"]) != OPEN:
            raise gl.vm.UserError("Pool is " + str(d["status"]))
        who = self._addr_hex(gl.message.sender_address).lower()
        if int(self._pos(pool_id, who)["coverage"]) <= 0:
            raise gl.vm.UserError("Only a covered address may file")
        tx = self._txid(txid)
        if not tx:
            raise gl.vm.UserError("txid must be 64 hex chars")

        p_tx = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": "eth_getTransactionByHash", "params": [tx]})
        p_rc = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": "eth_getTransactionReceipt", "params": [tx]})
        calls = [(RPC_A + "tx", p_tx), (RPC_A + "rc", p_rc),
                 (RPC_B + "tx", p_tx), (RPC_B + "rc", p_rc)]

        def run() -> dict:
            bodies = []
            for url, payload in calls:
                body = ""
                try:
                    body = self._body(gl.nondet.web.post(url, body=payload, headers=HDR))
                except Exception:
                    body = ""
                bodies.append(body)
            a = self._rpc_tx(bodies[0], bodies[1], tx)
            b = self._rpc_tx(bodies[2], bodies[3], tx)
            reason = self._verdict(a, b, d)
            return {"a": [a["found"], a["from"], a["to"], a["value"], a["block"], a["ok"]],
                    "b": [b["found"], b["from"], b["to"], b["value"], b["block"], b["ok"]],
                    "matched": reason == "", "reason": reason}

        def agrees(leader) -> bool:
            return isinstance(leader, gl.vm.Return) and self._agree(leader.calldata, run())

        res = gl.vm.run_nondet_unsafe(run, agrees)
        if not isinstance(res, dict):
            res = {}
        self.evidence[pool_id] = json.dumps(res)

        if res.get("matched") is True:
            d["status"] = TRIGGERED
            d["reason"] = "the covered address sent the threshold inside the window"
            d["event_txid"] = tx
            self._save(d)
            return TRIGGERED

        d["reason"] = str(res.get("reason", "the transaction could not be read"))
        self._save(d)
        return "NOT_PROVEN"

    @gl.public.write
    def claim(self, pool_id: str) -> str:
        """Pay a covered address its pro-rata share of the funded pool."""
        d = self._load(pool_id)
        if str(d["status"]) != TRIGGERED:
            raise gl.vm.UserError("Pool is " + str(d["status"]))
        who = self._addr_hex(gl.message.sender_address).lower()
        p = self._pos(pool_id, who)
        cov = int(p["coverage"])
        if cov <= 0:
            raise gl.vm.UserError("No cover to claim")
        if p["claimed"] is True:
            raise gl.vm.UserError("Already claimed")
        payout = cov * int(d["funded"]) // int(d["coverage_total"])
        if payout > cov:
            payout = cov
        p["claimed"] = True
        p["paid"] = payout
        self._put_pos(pool_id, p)
        d["paid_total"] = int(d["paid_total"]) + payout
        d["claimed_coverage"] = int(d["claimed_coverage"]) + cov
        self._save(d)
        self._transfer(Address(who), payout)
        return "CLAIMED"

    @gl.public.write
    def cancel(self, pool_id: str) -> str:
        """The underwriter withdraws a pool that sold nothing, before it starts."""
        d = self._load(pool_id)
        if str(d["status"]) != OPEN:
            raise gl.vm.UserError("Pool is " + str(d["status"]))
        who = self._addr_hex(gl.message.sender_address).lower()
        if who != str(d["underwriter"]).lower():
            raise gl.vm.UserError("Only the underwriter cancels")
        if int(d["coverage_total"]) != 0:
            raise gl.vm.UserError("Cover was sold")
        if not self._tip_ok(int(d["from_block"]), True):
            raise gl.vm.UserError("The window has already started")
        held = self._held(d)
        d["status"] = CLOSED
        d["reason"] = "cancelled before the window opened"
        d["refunded"] = held
        self._save(d)
        self._transfer(Address(who), held)
        return CLOSED

    @gl.public.write
    def close(self, pool_id: str) -> str:
        """Return what is left to the underwriter, once nothing more can pay."""
        d = self._load(pool_id)
        status = str(d["status"])
        if status == CLOSED:
            raise gl.vm.UserError("Pool is already closed")
        if status == OPEN:
            # the window is over, plus a filing grace nobody can shorten
            if not self._tip_ok(int(d["to_block"]) + FILING_BLOCKS, False):
                raise gl.vm.UserError("The filing period has not passed")
        elif int(d["claimed_coverage"]) != int(d["coverage_total"]):
            # unclaimed shares stay reserved until everyone claims or it lapses
            if not self._tip_ok(int(d["to_block"]) + LAPSE_BLOCKS, False):
                raise gl.vm.UserError("Covered addresses can still claim")
        held = self._held(d)
        d["status"] = CLOSED
        d["refunded"] = held
        if not str(d["reason"]):
            d["reason"] = "no covered transfer was proven"
        self._save(d)
        self._transfer(Address(d["underwriter"]), held)
        return CLOSED

    # ---------------- views ----------------

    @gl.public.view
    def get_pool(self, pool_id: str) -> str:
        d = self._load(pool_id)
        cap = int(d["capacity"])
        d["held"] = 0 if str(d["status"]) == CLOSED else self._held(d)
        d["collateral_bps"] = int(d["funded"]) * 10000 // cap if cap > 0 else 0
        return json.dumps(d)

    @gl.public.view
    def get_position(self, pool_id: str, holder: str) -> str:
        self._load(pool_id)
        p = self._pos(pool_id, self._addr_hex(holder))
        if int(p["coverage"]) <= 0:
            return "{}"
        return json.dumps(p)

    @gl.public.view
    def get_evidence(self, pool_id: str) -> str:
        self._load(pool_id)
        return str(self.evidence.get(str(pool_id), "")) or "{}"

    @gl.public.view
    def get_pool_count(self) -> int:
        return int(self.pool_count)

    @gl.public.view
    def quote(self, pool_id: str, coverage: int) -> str:
        d = self._load(pool_id)
        try:
            cov = int(coverage)
        except Exception:
            cov = 0
        prem = cov * int(d["premium_bps"]) // 10000 if cov > 0 else 0
        return json.dumps({"coverage": cov, "premium": prem,
                           "fits": int(d["coverage_total"]) + cov <= int(d["capacity"]),
                           "status": str(d["status"])})

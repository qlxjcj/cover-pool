"""Fixtures for Cover Pool direct-mode tests.

Both providers answer the same JSON-RPC envelope, so one parser handles both:
`eth_getTransactionByHash` gives blockNumber / from / to / value, and
`eth_getTransactionReceipt` gives status. The trailing `?m=` marker keeps the
two calls per provider apart (and is ignored by the RPC servers).

Direct mode does not track native value flow, so the same two accounting hooks
are installed, and a refused payable call puts the value back.
"""

import json
import os

import pytest

CONTRACT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "cover_pool.py",
)

UNDERWRITER = "0x" + "aa" * 20
BUYER1 = "0x" + "bb" * 20
BUYER2 = "0x" + "cc" * 20
STRANGER = "0x" + "44" * 20
TRIGGER = "0x" + "11" * 20
DEST = "0x" + "22" * 20

TXID = "ab" * 32
TIP = 256
FROM_BLOCK = 300
TO_BLOCK = 400
TX_BLOCK = 350
VALUE = 10 ** 18                 # 1 ETH in wei, the watched amount
THRESHOLD = 10 ** 18

FUNDED = 5 * 10 ** 15            # 0.005 GEN
CAPACITY = 2 * 10 ** 16          # 0.02 GEN
BPS = 500                        # 5% of coverage
AFTER_FILING = TO_BLOCK + 1200 + 10


def hx(n: int) -> str:
    return hex(int(n))


def tx_body(txid=TXID, frm=TRIGGER, to=DEST, value=VALUE, block=TX_BLOCK):
    # RPC providers return and expect the 0x-prefixed form of the hash
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "hash": "0x" + txid, "blockNumber": hx(block), "from": frm, "to": to,
        "value": hex(value)}})


def rc_body(txid=TXID, ok=True):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "transactionHash": "0x" + txid, "status": "0x1" if ok else "0x0"}})


def remock(vm, a_tx=None, a_rc=None, b_tx=None, b_rc=None, tip_a=TIP, tip_b=TIP):
    vm.clear_mocks()
    vm.mock_web(r".*publicnode\.com/\?m=tx.*",
                {"method": "POST", "status": 200,
                 "body": a_tx if a_tx is not None else tx_body()})
    vm.mock_web(r".*publicnode\.com/\?m=rc.*",
                {"method": "POST", "status": 200,
                 "body": a_rc if a_rc is not None else rc_body()})
    vm.mock_web(r".*drpc\.org/\?m=tx.*",
                {"method": "POST", "status": 200,
                 "body": b_tx if b_tx is not None else tx_body()})
    vm.mock_web(r".*drpc\.org/\?m=rc.*",
                {"method": "POST", "status": 200,
                 "body": b_rc if b_rc is not None else rc_body()})
    vm.mock_web(r".*publicnode\.com/\?m=tip.*",
                {"method": "POST", "status": 200,
                 "body": json.dumps({"jsonrpc": "2.0", "id": 1, "result": hx(tip_a)})})
    vm.mock_web(r".*drpc\.org/\?m=tip.*",
                {"method": "POST", "status": 200,
                 "body": json.dumps({"jsonrpc": "2.0", "id": 1, "result": hx(tip_b)})})


def happy(vm, **over):
    remock(vm,
           a_tx=over.get("a_tx"), a_rc=over.get("a_rc"),
           b_tx=over.get("b_tx"), b_rc=over.get("b_rc"),
           tip_a=over.get("tip_a", TIP), tip_b=over.get("tip_b", TIP))


@pytest.fixture
def pool(direct_vm, direct_deploy):
    vm = direct_vm

    def _value_transfer_hook(vm, request):
        if "PostMessage" not in request:
            return None
        msg = request["PostMessage"]
        amount = int(msg.get("value", 0))
        if amount > 0:
            contract = vm._contract_address
            recipient = vm._to_bytes(msg["address"])
            vm._balances[contract] = vm._balances.get(contract, 0) - amount
            vm._balances[recipient] = vm._balances.get(recipient, 0) + amount
        return {"ok": None}

    vm._gl_call_hook = _value_transfer_hook

    remock(vm)
    c = direct_deploy(CONTRACT)

    def _wrap(fn):
        def inner(*args):
            paid = 0
            sender = vm._to_bytes(vm.sender)
            contract = vm._contract_address
            if vm.value > 0:
                vm._balances[sender] = vm._balances.get(sender, 0) - vm.value
                vm._balances[contract] = vm._balances.get(contract, 0) + vm.value
                paid = vm.value
            try:
                return fn(*args)
            except Exception:
                if paid:
                    vm._balances[sender] = vm._balances.get(sender, 0) + paid
                    vm._balances[contract] = vm._balances.get(contract, 0) - paid
                raise
        return inner

    c.open_pool = _wrap(c.open_pool)
    c.buy_cover = _wrap(c.buy_cover)

    for addr in (UNDERWRITER, BUYER1, BUYER2, STRANGER):
        vm.deal(addr, 1000 * FUNDED)
    return vm, c


def balance(vm, addr):
    return vm._balances.get(vm._to_bytes(addr), 0)


def contract_balance(vm):
    return vm._balances.get(vm._contract_address, 0)


def open_pool(vm, c, sender=UNDERWRITER, funded=FUNDED,
              trigger=TRIGGER, threshold=THRESHOLD, from_block=FROM_BLOCK,
              to_block=TO_BLOCK, bps=BPS, capacity=CAPACITY):
    vm.sender = sender
    vm.value = funded
    try:
        return c.open_pool(trigger, threshold, from_block, to_block, bps, capacity)
    finally:
        vm.value = 0


def buy(vm, c, pid, coverage, sender=BUYER1):
    vm.sender = sender
    vm.value = coverage * BPS // 10000
    try:
        return c.buy_cover(pid, coverage)
    finally:
        vm.value = 0


def observe(vm, c, pid, txid=TXID, sender=BUYER1):
    vm.sender = sender
    return c.observe(pid, txid)


def claim(vm, c, pid, sender=BUYER1):
    vm.sender = sender
    return c.claim(pid)


def close(vm, c, pid, sender=STRANGER):
    vm.sender = sender
    return c.close(pid)


def get_pool(vm, c, pid):
    return json.loads(c.get_pool(pid))


def position(vm, c, pid, who):
    return json.loads(c.get_position(pid, who))


def evidence(vm, c, pid):
    return json.loads(c.get_evidence(pid))


def snapshot(vm, c, pid):
    return (c.get_pool(pid), c.get_evidence(pid),
            c.get_position(pid, BUYER1), c.get_position(pid, BUYER2))

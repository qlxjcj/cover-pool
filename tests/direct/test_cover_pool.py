"""Direct-mode tests for Cover Pool.

Groups:

  A  units       -- GEN wei escrow, ETH wei threshold, both declared, never priced
  B  opening     -- the event and the window are frozen before anyone buys
  C  buying      -- exact premium, capacity, and sales close on the block clock
  D  observing   -- two indexers must agree on sender/recipient/value/block/success
  E  agreement   -- validators bind exactly the facts that decide the trigger
  F  claims      -- pro-rata from solvency, capped at coverage, once each
  G  exits       -- cancel before the window, close after it, lapse for stragglers
  H  invariants  -- refusals change nothing, money is conserved, views are strings
"""

import json

import pytest

from conftest import (
    AFTER_FILING,
    BPS,
    BUYER1,
    BUYER2,
    CAPACITY,
    DEST,
    FROM_BLOCK,
    FUNDED,
    STRANGER,
    THRESHOLD,
    TIP,
    TO_BLOCK,
    TRIGGER,
    TXID,
    TX_BLOCK,
    UNDERWRITER,
    VALUE,
    balance,
    bs_body,
    buy,
    claim,
    close,
    contract_balance,
    evidence,
    happy,
    observe,
    open_pool,
    get_pool,
    position,
    remock,
    rs_rc_body,
    rs_tx_body,
    snapshot,
)

COVER = 10 ** 16                 # 0.01 GEN
PREMIUM = COVER * BPS // 10000   # 5e14 wei


# ==========================================================================
# A. units
# ==========================================================================

def test_opening_escrows_the_gen_amount(pool):
    vm, c = pool
    before = balance(vm, UNDERWRITER)
    pid = open_pool(vm, c)
    d = get_pool(vm, c, pid)
    assert contract_balance(vm) == FUNDED
    assert balance(vm, UNDERWRITER) == before - FUNDED
    assert d["funded"] == FUNDED
    assert d["paid_total"] == 0 and d["refunded"] == 0


def test_the_pool_declares_both_scales(pool):
    vm, c = pool
    d = get_pool(vm, c, open_pool(vm, c))
    assert d["units"] == {"asset": "GEN", "amount_decimals": 18,
                          "watched_chain": "ethereum", "watched_unit": "wei",
                          "watched_decimals": 18}
    assert d["threshold"] == THRESHOLD      # ETH wei, compared with the transfer
    assert d["funded"] == FUNDED            # GEN wei, the escrow
    assert d["premium_bps"] == BPS


def test_a_refused_open_moves_no_funds(pool):
    vm, c = pool
    before = balance(vm, UNDERWRITER)
    with pytest.raises(Exception):
        open_pool(vm, c, funded=0)
    assert balance(vm, UNDERWRITER) == before
    assert contract_balance(vm) == 0
    assert c.get_pool_count() == 0


# ==========================================================================
# B. opening
# ==========================================================================

def test_the_event_and_window_are_frozen(pool):
    vm, c = pool
    d = get_pool(vm, c, open_pool(vm, c))
    assert d["trigger_address"] == TRIGGER
    assert d["threshold"] == THRESHOLD
    assert d["from_block"] == FROM_BLOCK
    assert d["to_block"] == TO_BLOCK
    assert d["status"] == "OPEN"
    assert d["underwriter"] == UNDERWRITER.lower()


def test_the_collateral_ratio_is_visible(pool):
    vm, c = pool
    d = get_pool(vm, c, open_pool(vm, c))
    assert d["collateral_bps"] == FUNDED * 10000 // CAPACITY
    assert d["held"] == FUNDED


@pytest.mark.parametrize("kw", [
    {"trigger": "0x1234"}, {"trigger": "0x" + "zz" * 20},
    {"threshold": 0}, {"from_block": 0}, {"to_block": 299},
    {"bps": 0}, {"bps": 10001}, {"capacity": 0},
])
def test_rejects_a_bad_pool(pool, kw):
    vm, c = pool
    with pytest.raises(Exception):
        open_pool(vm, c, **kw)
    assert c.get_pool_count() == 0
    assert contract_balance(vm) == 0


def test_a_window_that_already_started_is_refused(pool):
    vm, c = pool
    with pytest.raises(Exception):
        open_pool(vm, c, from_block=TIP)
    assert c.get_pool_count() == 0


def test_an_unreadable_tip_blocks_the_open(pool):
    vm, c = pool
    remock(vm, bs_tip=0)
    with pytest.raises(Exception):
        open_pool(vm, c)
    assert c.get_pool_count() == 0


# ==========================================================================
# C. buying
# ==========================================================================

def test_a_buyer_pays_exactly_the_premium(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    before = balance(vm, BUYER1)
    assert buy(vm, c, pid, COVER) == "COVERED"
    assert balance(vm, BUYER1) == before - PREMIUM
    assert contract_balance(vm) == FUNDED + PREMIUM
    d = get_pool(vm, c, pid)
    assert d["coverage_total"] == COVER
    assert d["premium_total"] == PREMIUM
    p = position(vm, c, pid, BUYER1)
    assert p["coverage"] == COVER and p["premium"] == PREMIUM
    assert p["claimed"] is False and p["paid"] == 0


def test_the_quote_is_what_gets_charged(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    q = json.loads(c.quote(pid, COVER))
    assert q == {"coverage": COVER, "premium": PREMIUM, "fits": True, "status": "OPEN"}


def test_a_wrong_value_is_refused(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    before = balance(vm, BUYER1)
    for value in (0, PREMIUM - 1, PREMIUM + 1):
        vm.sender = BUYER1
        vm.value = value
        try:
            with pytest.raises(Exception):
                c.buy_cover(pid, COVER)
        finally:
            vm.value = 0
    assert balance(vm, BUYER1) == before
    assert get_pool(vm, c, pid)["coverage_total"] == 0


def test_capacity_cannot_be_exceeded(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    buy(vm, c, pid, CAPACITY, sender=BUYER1)
    with pytest.raises(Exception):
        buy(vm, c, pid, 1, sender=BUYER2)
    assert get_pool(vm, c, pid)["coverage_total"] == CAPACITY


def test_the_underwriter_cannot_buy_its_own_pool(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    with pytest.raises(Exception):
        buy(vm, c, pid, COVER, sender=UNDERWRITER)


def test_the_covered_address_cannot_buy(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    with pytest.raises(Exception):
        buy(vm, c, pid, COVER, sender=TRIGGER)


def test_a_buyer_may_top_up(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    buy(vm, c, pid, COVER)
    p = position(vm, c, pid, BUYER1)
    assert p["coverage"] == 2 * COVER
    assert get_pool(vm, c, pid)["coverage_total"] == 2 * COVER


def test_a_premium_that_rounds_to_zero_is_refused(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    vm.sender = BUYER1
    vm.value = 0
    with pytest.raises(Exception):
        c.buy_cover(pid, 1)
    assert get_pool(vm, c, pid)["coverage_total"] == 0


def test_sales_stop_when_the_window_starts(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    # the fresher tip has already reached the first covered block
    remock(vm, bs_tip=FROM_BLOCK)
    with pytest.raises(Exception):
        buy(vm, c, pid, COVER)
    assert get_pool(vm, c, pid)["coverage_total"] == 0


# ==========================================================================
# D. observing
# ==========================================================================

def test_two_indexers_that_agree_trigger_the_pool(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    assert observe(vm, c, pid) == "TRIGGERED"
    d = get_pool(vm, c, pid)
    assert d["status"] == "TRIGGERED"
    assert d["event_txid"] == TXID
    assert "threshold" in d["reason"]


def test_the_evidence_records_both_sides(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    observe(vm, c, pid)
    ev = evidence(vm, c, pid)
    assert ev["matched"] is True
    assert ev["a"] == [True, TRIGGER, DEST, VALUE, TX_BLOCK, True]
    assert ev["b"] == ev["a"]


def test_only_a_covered_address_may_file(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    with pytest.raises(Exception):
        observe(vm, c, pid, sender=STRANGER)
    with pytest.raises(Exception):
        observe(vm, c, pid, sender=UNDERWRITER)
    assert get_pool(vm, c, pid)["status"] == "OPEN"


def test_the_txid_must_be_64_hex(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    for bad in ("", "abc", "zz" * 32, "ab" * 31):
        with pytest.raises(Exception):
            observe(vm, c, pid, txid=bad)


def test_an_unknown_source_fails_closed(pool):
    vm, c = pool
    remock(vm, bs=bs_body(txid="00" * 32), tx=rs_tx_body(txid="00" * 32),
           rc=rs_rc_body(txid="00" * 32))
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    assert observe(vm, c, pid) == "NOT_PROVEN"
    assert get_pool(vm, c, pid)["reason"] == "blockscout has no such transaction"


def test_a_second_indexer_that_does_not_know_it_fails_closed(pool):
    vm, c = pool
    remock(vm, bs=bs_body(), tx=rs_tx_body(txid="11" * 32),
           rc=rs_rc_body(txid="11" * 32))
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    assert observe(vm, c, pid) == "NOT_PROVEN"
    assert get_pool(vm, c, pid)["reason"] == "routescan has no such transaction"


@pytest.mark.parametrize("over,reason", [
    ({"tx": rs_tx_body(block=TX_BLOCK + 1), "rc": rs_rc_body(block=TX_BLOCK + 1)},
     "the indexers disagree on the block"),
    ({"tx": rs_tx_body(to="0x" + "33" * 20)}, "the indexers disagree on the transfer"),
    ({"tx": rs_tx_body(value=VALUE + 1)}, "the indexers disagree on the value"),
    ({"bs": bs_body(result="error")}, "the indexers disagree on success"),
])
def test_any_fact_disagreement_fails_closed(pool, over, reason):
    vm, c = pool
    happy(vm, **over)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    assert observe(vm, c, pid) == "NOT_PROVEN"
    assert get_pool(vm, c, pid)["reason"] == reason
    assert get_pool(vm, c, pid)["status"] == "OPEN"


def test_a_failed_transfer_cannot_trigger(pool):
    vm, c = pool
    happy(vm, bs=bs_body(result="error"), rc=rs_rc_body(ok=False))
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    assert observe(vm, c, pid) == "NOT_PROVEN"
    assert get_pool(vm, c, pid)["reason"] == "the transaction failed"


@pytest.mark.parametrize("over,reason", [
    ({"bs": bs_body(block=FROM_BLOCK - 1), "tx": rs_tx_body(block=FROM_BLOCK - 1)},
     "the transfer is before the window"),
    ({"bs": bs_body(block=TO_BLOCK + 1), "tx": rs_tx_body(block=TO_BLOCK + 1)},
     "the transfer is after the window"),
    ({"bs": bs_body(frm="0x" + "55" * 20), "tx": rs_tx_body(frm="0x" + "55" * 20)},
     "the sender is not the covered address"),
    ({"bs": bs_body(to=TRIGGER), "tx": rs_tx_body(to=TRIGGER)},
     "the transfer does not send funds away"),
    ({"bs": bs_body(value=THRESHOLD - 1), "tx": rs_tx_body(value=THRESHOLD - 1)},
     "the transfer is below the threshold"),
])
def test_a_transfer_that_does_not_meet_the_terms_is_refused(pool, over, reason):
    vm, c = pool
    happy(vm, **over)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    assert observe(vm, c, pid) == "NOT_PROVEN"
    assert get_pool(vm, c, pid)["reason"] == reason


def test_a_refusal_is_not_final(pool):
    vm, c = pool
    remock(vm, bs=bs_body(frm="0x" + "55" * 20),
           tx=rs_tx_body(frm="0x" + "55" * 20), rc=rs_rc_body())
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    assert observe(vm, c, pid) == "NOT_PROVEN"
    happy(vm)
    assert observe(vm, c, pid) == "TRIGGERED"


def test_a_triggered_pool_takes_no_more_evidence(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    observe(vm, c, pid)
    with pytest.raises(Exception):
        observe(vm, c, pid)
    with pytest.raises(Exception):
        buy(vm, c, pid, COVER)


# ==========================================================================
# E. agreement
# ==========================================================================

def result(a=None, b=None, matched=True, reason=""):
    fact = a or [True, TRIGGER, DEST, VALUE, TX_BLOCK, True]
    return {"a": fact, "b": b or fact, "matched": matched, "reason": reason}


def test_identical_runs_agree(pool):
    vm, c = pool
    r = result()
    assert c._agree(r, r) is True


@pytest.mark.parametrize("other", [
    result(b=[True, TRIGGER, DEST, VALUE, TX_BLOCK + 1, True]),
    result(b=[True, TRIGGER, DEST, VALUE + 1, TX_BLOCK, True]),
    result(b=[True, TRIGGER, "0x" + "66" * 20, VALUE, TX_BLOCK, True]),
    result(b=[True, TRIGGER, DEST, VALUE, TX_BLOCK, False]),
    result(matched=False, reason="the transaction failed"),
])
def test_any_different_fact_is_refused(pool, other):
    vm, c = pool
    assert c._agree(result(), other) is False


def test_missing_sides_are_refused(pool):
    vm, c = pool
    assert c._agree({"matched": True}, result()) is False
    assert c._agree(None, result()) is False
    assert c._agree(result(), "nope") is False


# ==========================================================================
# F. claims
# ==========================================================================

def test_a_shortfall_is_shared_pro_rata(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)          # funded 0.005, capacity 0.02
    buy(vm, c, pid, COVER, sender=BUYER1)
    buy(vm, c, pid, COVER, sender=BUYER2)
    observe(vm, c, pid, sender=BUYER1)
    b1 = balance(vm, BUYER1)
    b2 = balance(vm, BUYER2)
    claim(vm, c, pid, sender=BUYER1)
    claim(vm, c, pid, sender=BUYER2)
    payout = COVER * FUNDED // (2 * COVER)
    assert balance(vm, BUYER1) == b1 + payout
    assert balance(vm, BUYER2) == b2 + payout
    d = get_pool(vm, c, pid)
    assert d["paid_total"] == 2 * payout == FUNDED
    assert contract_balance(vm) == FUNDED + 2 * PREMIUM - FUNDED


def test_a_funded_pool_pays_full_coverage(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c, funded=2 * COVER, capacity=4 * COVER)
    buy(vm, c, pid, COVER)
    observe(vm, c, pid)
    before = balance(vm, BUYER1)
    claim(vm, c, pid)
    assert balance(vm, BUYER1) == before + COVER
    assert position(vm, c, pid, BUYER1)["paid"] == COVER


def test_a_covered_address_claims_once(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    observe(vm, c, pid)
    claim(vm, c, pid)
    before = balance(vm, BUYER1)
    with pytest.raises(Exception):
        claim(vm, c, pid)
    assert balance(vm, BUYER1) == before


def test_a_stranger_cannot_claim(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    observe(vm, c, pid)
    with pytest.raises(Exception):
        claim(vm, c, pid, sender=STRANGER)
    with pytest.raises(Exception):
        claim(vm, c, pid, sender=UNDERWRITER)


def test_nothing_is_paid_before_the_trigger(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    with pytest.raises(Exception):
        claim(vm, c, pid)
    assert get_pool(vm, c, pid)["paid_total"] == 0


# ==========================================================================
# G. exits
# ==========================================================================

def test_the_underwriter_cancels_a_pool_that_sold_nothing(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    before = balance(vm, UNDERWRITER)
    vm.sender = UNDERWRITER
    assert c.cancel(pid) == "CLOSED"
    assert balance(vm, UNDERWRITER) == before + FUNDED
    assert get_pool(vm, c, pid)["refunded"] == FUNDED
    assert contract_balance(vm) == 0


def test_a_pool_with_cover_cannot_be_cancelled(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    vm.sender = UNDERWRITER
    with pytest.raises(Exception):
        c.cancel(pid)


def test_only_the_underwriter_cancels(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.cancel(pid)


def test_closing_an_open_pool_waits_for_the_window(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    with pytest.raises(Exception):
        close(vm, c, pid)
    remock(vm, bs_tip=TO_BLOCK + 1)
    with pytest.raises(Exception):
        close(vm, c, pid)
    remock(vm, bs_tip=AFTER_FILING)
    assert close(vm, c, pid) == "CLOSED"


def test_an_untriggered_pool_returns_everything(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    remock(vm, bs_tip=AFTER_FILING)
    before = balance(vm, UNDERWRITER)
    assert close(vm, c, pid) == "CLOSED"
    assert balance(vm, UNDERWRITER) == before + FUNDED + PREMIUM
    d = get_pool(vm, c, pid)
    assert d["refunded"] == FUNDED + PREMIUM
    assert d["paid_total"] == 0
    assert d["held"] == 0
    assert contract_balance(vm) == 0


def test_unclaimed_shares_are_reserved_then_lapse(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    observe(vm, c, pid)
    remock(vm, bs_tip=AFTER_FILING)          # window over, cover still claimable
    with pytest.raises(Exception):
        close(vm, c, pid)
    remock(vm, bs_tip=TO_BLOCK + 100000 + 10)  # the lapse has passed
    before = balance(vm, UNDERWRITER)
    assert close(vm, c, pid) == "CLOSED"
    assert balance(vm, UNDERWRITER) == before + FUNDED + PREMIUM
    assert contract_balance(vm) == 0


def test_a_fully_claimed_pool_closes_at_once(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    observe(vm, c, pid)
    claim(vm, c, pid)
    before = balance(vm, UNDERWRITER)
    assert close(vm, c, pid) == "CLOSED"
    assert balance(vm, UNDERWRITER) == before + PREMIUM
    assert contract_balance(vm) == 0


def test_a_closed_pool_cannot_move_again(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    remock(vm, bs_tip=AFTER_FILING)
    close(vm, c, pid)
    with pytest.raises(Exception):
        close(vm, c, pid)
    with pytest.raises(Exception):
        buy(vm, c, pid, COVER)


def test_two_pools_never_mix_funds(pool):
    vm, c = pool
    happy(vm)
    a = open_pool(vm, c)
    b = open_pool(vm, c, funded=2 * FUNDED, capacity=4 * COVER)
    buy(vm, c, a, COVER, sender=BUYER1)
    buy(vm, c, b, COVER, sender=BUYER2)
    observe(vm, c, a, sender=BUYER1)
    claim(vm, c, a, sender=BUYER1)
    remock(vm, bs_tip=AFTER_FILING)
    close(vm, c, b)
    assert get_pool(vm, c, b)["paid_total"] == 0
    assert get_pool(vm, c, b)["refunded"] == 2 * FUNDED + PREMIUM
    assert get_pool(vm, c, a)["paid_total"] == FUNDED   # one buyer, still a shortfall


# ==========================================================================
# H. invariants and views
# ==========================================================================

def test_every_refusal_leaves_the_state_byte_identical(pool):
    vm, c = pool
    happy(vm)
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    before = snapshot(vm, c, pid)
    with pytest.raises(Exception):
        observe(vm, c, pid, sender=STRANGER)
    with pytest.raises(Exception):
        claim(vm, c, pid)
    with pytest.raises(Exception):
        close(vm, c, pid)
    with pytest.raises(Exception):
        buy(vm, c, pid, CAPACITY * 10)
    assert snapshot(vm, c, pid) == before


def test_money_is_conserved_on_every_path(pool):
    vm, c = pool
    happy(vm)
    paid = open_pool(vm, c)
    buy(vm, c, paid, COVER)
    observe(vm, c, paid)
    claim(vm, c, paid)
    close(vm, c, paid)

    quiet = open_pool(vm, c)
    buy(vm, c, quiet, COVER)
    remock(vm, bs_tip=AFTER_FILING)
    close(vm, c, quiet)
    for pid in (paid, quiet):
        d = get_pool(vm, c, pid)
        assert d["paid_total"] + d["refunded"] == d["funded"] + d["premium_total"]
    assert contract_balance(vm) == 0


def test_views_return_strings(pool):
    vm, c = pool
    pid = open_pool(vm, c)
    buy(vm, c, pid, COVER)
    for v in (c.get_pool(pid), c.get_position(pid, BUYER1),
              c.get_evidence(pid), c.quote(pid, COVER)):
        assert isinstance(v, str)
    assert json.loads(c.get_position(pid, BUYER1))["coverage"] == COVER
    assert c.get_position(pid, STRANGER) == "{}"
    assert c.get_evidence(pid) == "{}"


def test_unknown_pools_raise(pool):
    vm, c = pool
    with pytest.raises(Exception):
        c.get_pool("p99")
    with pytest.raises(Exception):
        c.get_position("p99", BUYER1)
    with pytest.raises(Exception):
        c.get_evidence("p99")


def test_pool_count_tracks_opens(pool):
    vm, c = pool
    assert c.get_pool_count() == 0
    open_pool(vm, c)
    open_pool(vm, c)
    assert c.get_pool_count() == 2

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_bot.dry_run import FakeBroker
from trading_bot.market_data import session_info
from trading_bot.risk_manager import RiskRejected, sellable_qty, validate_new_intent
from trading_bot.safety import BrokerStopUnsupported, OrderValidationError, validate_order


def intent(**overrides):
    value = {
        "symbol": "CRWV", "side": "buy", "qty": 3, "limit_price": 100.05,
        "order_type": "limit", "time_in_force": "day", "extended_hours": False,
        "stop_policy": "NONE", "stop_loss": None,
    }
    value.update(overrides)
    return value


QUOTE = {"bid": 99.99, "ask": 100.01, "last": 100.0,
         "quote_age_seconds": 0.0, "last_age_seconds": 0.0}


def account(**overrides):
    value = FakeBroker().snapshot()
    value.update(overrides)
    return value


def validate(intent_value, bundle, session, *, account_value=None, buy_attempts=0,
             local_orders=None, freeze_reason="", stop_allowed=False):
    return validate_new_intent(
        intent_value,
        account=account_value or account(),
        quote=QUOTE,
        risk=bundle.risk,
        execution=bundle.execution,
        symbols=bundle.symbols,
        session=session,
        buy_attempts=buy_attempts,
        local_orders=local_orders or [],
        freeze_reason=freeze_reason,
        stop_allowed=stop_allowed,
    )


def test_ai_large_qty_is_not_silently_reduced(bundle, session):
    qty = validate(intent(qty=900), bundle, session)
    assert qty == 900


def test_insufficient_cash_rejects_instead_of_resizing(bundle, session):
    with pytest.raises(RiskRejected) as failure:
        validate(intent(qty=1001), bundle, session)
    assert failure.value.code == "INSUFFICIENT_CASH"


def test_cash_reserved_for_open_buy_orders(bundle, session):
    acct = account(open_orders=[{
        "symbol": "CRWV", "side": "buy", "qty": "900", "filled_qty": "0",
        "limit_price": "100.00", "status": "new", "type": "limit",
    }])
    with pytest.raises(RiskRejected) as failure:
        validate(intent(qty=200), bundle, session, account_value=acct)
    assert failure.value.code == "INSUFFICIENT_CASH"


def test_sell_respects_existing_sell_and_stop_reservations(bundle, session):
    acct = account(
        positions={"CRWV": 1000}, quantities={"CRWV": 10},
        available_quantities={"CRWV": 10},
        open_orders=[{
            "symbol": "CRWV", "side": "sell", "qty": "4", "filled_qty": "0",
            "status": "new", "type": "limit",
        }],
    )
    with pytest.raises(RiskRejected) as failure:
        validate(intent(side="sell", qty=8), bundle, session, account_value=acct)
    assert failure.value.code == "INSUFFICIENT_SHARES"
    assert sellable_qty(acct, "CRWV") == 6
    assert validate(intent(side="sell", qty=6), bundle, session, account_value=acct) == 6


def test_fractional_residual_cannot_be_sold(bundle, session):
    acct = account(positions={"CRWV": 1090}, quantities={"CRWV": 10.9},
                   available_quantities={"CRWV": 10.9})
    with pytest.raises(RiskRejected) as failure:
        validate(intent(side="sell", qty=11), bundle, session, account_value=acct)
    assert failure.value.code == "INSUFFICIENT_SHARES"
    assert validate(intent(side="sell", qty=10), bundle, session, account_value=acct) == 10


def test_daily_loss_blocks_buys_but_allows_reduction(bundle, session):
    acct = account(equity=100000, last_equity=110000, positions={"CRWV": 1000},
                   quantities={"CRWV": 10}, available_quantities={"CRWV": 10})
    with pytest.raises(RiskRejected) as failure:
        validate(intent(), bundle, session, account_value=acct)
    assert failure.value.code == "DAILY_LOSS_LIMIT"
    assert validate(intent(side="sell", qty=10), bundle, session, account_value=acct) == 10


def test_daily_order_limit_blocks_buys_but_allows_reduction(bundle, session):
    acct = account(positions={"CRWV": 1000}, quantities={"CRWV": 10},
                   available_quantities={"CRWV": 10})
    with pytest.raises(RiskRejected) as failure:
        validate(intent(), bundle, session, account_value=acct, buy_attempts=10)
    assert failure.value.code == "DAILY_ORDER_LIMIT"
    assert validate(intent(side="sell", qty=10), bundle, session,
                    account_value=acct, buy_attempts=10) == 10


def test_frozen_and_outside_window_reject(bundle, session):
    with pytest.raises(RiskRejected) as failure:
        validate(intent(), bundle, session, freeze_reason="ORDER_UNKNOWN")
    assert failure.value.code == "FROZEN"
    with pytest.raises(RiskRejected) as failure:
        validate_new_intent(
            intent(), account=account(), quote=QUOTE, risk=bundle.risk,
            execution=bundle.execution, symbols=bundle.symbols, session=session,
            buy_attempts=0, paused=True,
        )
    assert failure.value.code == "PAUSED"
    after = session_info(datetime(2026, 11, 5, 15, 5, tzinfo=timezone.utc), bundle.execution)
    with pytest.raises(RiskRejected) as failure:
        validate(intent(), bundle, after)
    assert failure.value.code in {"OUTSIDE_ACTIVE_WINDOW", "AFTER_SUBMIT_CUTOFF"}


def test_conflicting_open_order_blocks_opposite_side(bundle, session):
    with pytest.raises(RiskRejected) as failure:
        validate(
            intent(), bundle, session,
            account_value=account(open_orders=[{
                "symbol": "CRWV", "side": "sell", "qty": "1", "filled_qty": "0",
                "status": "new", "type": "limit",
            }]),
        )
    assert failure.value.code == "CONFLICTING_ORDER"
    with pytest.raises(RiskRejected) as failure:
        validate(
            intent(side="sell", qty=1), bundle, session,
            account_value=account(
                positions={"CRWV": 1000}, quantities={"CRWV": 10},
                available_quantities={"CRWV": 10},
                open_orders=[{
                    "symbol": "CRWV", "side": "buy", "qty": "1", "filled_qty": "0",
                    "status": "new", "type": "limit",
                }],
            ),
        )
    assert failure.value.code == "CONFLICTING_ORDER"


def test_stop_leg_reserves_protected_shares(bundle, session):
    acct = account(
        positions={"CRWV": 1000}, quantities={"CRWV": 10},
        available_quantities={"CRWV": 10},
        open_orders=[{
            "symbol": "CRWV", "side": "buy", "qty": "4", "filled_qty": "4",
            "status": "partially_filled", "type": "limit", "order_class": "oto",
            "legs": [{"type": "stop", "side": "sell", "qty": "4", "filled_qty": "0",
                      "status": "new"}],
        }],
    )
    with pytest.raises(RiskRejected) as failure:
        validate(intent(side="sell", qty=7), bundle, session, account_value=acct)
    assert failure.value.code == "CONFLICTING_ORDER" or failure.value.code == "INSUFFICIENT_SHARES"
    assert sellable_qty(acct, "CRWV") == 6


def test_broker_stop_is_rejected_not_downgraded(bundle, session):
    with pytest.raises(BrokerStopUnsupported):
        validate(intent(stop_policy="BROKER_STOP", stop_loss={"stop_price": 95}),
                 bundle, session)
    result = validate(intent(stop_policy="BROKER_STOP", stop_loss={"stop_price": 95}),
                      bundle, session, stop_allowed=True)
    assert result == 3


def test_none_must_not_carry_stop(bundle, session):
    with pytest.raises((RiskRejected, OrderValidationError)):
        validate(intent(stop_loss={"stop_price": 95}), bundle, session)


def test_sell_broker_stop_is_rejected(bundle, session):
    with pytest.raises((RiskRejected, BrokerStopUnsupported, OrderValidationError)):
        validate(intent(side="sell", qty=1, stop_policy="BROKER_STOP",
                        stop_loss={"stop_price": 105}), bundle, session, stop_allowed=True)


def test_order_contract_is_day_limit_whole_shares():
    assert validate_order({
        "symbol": "CRWV", "side": "buy", "qty": 3, "limit_price": 10.5,
        "type": "limit", "time_in_force": "day",
    }) == 3
    with pytest.raises(OrderValidationError):
        validate_order({
            "symbol": "CRWV", "side": "buy", "qty": 3, "limit_price": 10.5,
            "type": "limit", "time_in_force": "ioc",
        })
    with pytest.raises(OrderValidationError):
        validate_order({
            "symbol": "CRWV", "side": "buy", "qty": 1.5, "limit_price": 10.5,
            "type": "limit", "time_in_force": "day",
        })
    with pytest.raises(OrderValidationError):
        validate_order({
            "symbol": "CRWV", "side": "buy", "qty": 3, "limit_price": 10.5,
            "type": "limit", "time_in_force": "day", "notional": 10,
        })


def test_quote_quality_blocks_bad_spread(bundle, session):
    with pytest.raises(RiskRejected):
        validate_new_intent(
            intent(), account=account(), quote={"bid": 1.0, "ask": 1.1, "last": 1.0,
                                                 "quote_age_seconds": 0.0},
            risk=bundle.risk, execution=bundle.execution, symbols=bundle.symbols,
            session=session, buy_attempts=0,
        )

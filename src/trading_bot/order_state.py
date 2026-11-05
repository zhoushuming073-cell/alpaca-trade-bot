"""Explicit, auditable local order lifecycle.

The local lifecycle is deliberately separate from the broker's order status.
Only the local states before ``DISPATCH_INTENT_RECORDED`` are proof that no
HTTP POST could have happened.  Everything after that boundary is provisional
until a broker fact (a looked-up order) says otherwise.

The state machine is intentionally small; the important property is not the
number of states but the ordering guarantee:

    RESERVED -> VALIDATING -> READY_TO_DISPATCH
             -> DISPATCH_INTENT_RECORDED  (durable "it may have been sent")
             -> broker facts / SUBMISSION_OUTCOME_UNKNOWN

A pre-dispatch failure can therefore be resolved as ``NOT_SENT`` without ever
asking the broker.  Conversely, a post-dispatch failure is never called
"not sent" merely because a lookup returned 404.
"""

from __future__ import annotations

from enum import StrEnum


class OrderState(StrEnum):
    # Local preparation.  None of these states allow a POST to have happened.
    RESERVED = "RESERVED"
    VALIDATING = "VALIDATING"
    READY_TO_DISPATCH = "READY_TO_DISPATCH"

    # Durable boundary: the process intended to POST.  A crash from here on
    # must be reconciled against the broker, never treated as "not sent".
    DISPATCH_INTENT_RECORDED = "DISPATCH_INTENT_RECORDED"
    SUBMISSION_OUTCOME_UNKNOWN = "SUBMISSION_OUTCOME_UNKNOWN"

    # Broker facts, normalised.
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"

    # An ambiguous or contradictory broker/local fact.  New risk is frozen
    # until a human (or the next authoritative reconciliation) resolves it.
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

    # Provably no POST happened.  This is a normal terminal outcome, not an
    # error and not an unresolved order.
    NOT_SENT = "NOT_SENT"


TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
        OrderState.NOT_SENT,
    }
)

# States that exist only before the durable dispatch boundary.
PRE_DISPATCH_STATES: frozenset[OrderState] = frozenset(
    {OrderState.RESERVED, OrderState.VALIDATING, OrderState.READY_TO_DISPATCH}
)

# States in which a broker order may exist and must be looked up.
POST_DISPATCH_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.DISPATCH_INTENT_RECORDED,
        OrderState.SUBMISSION_OUTCOME_UNKNOWN,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.RECONCILIATION_REQUIRED,
    }
)

# A broker order is still alive (may fill later).
ACTIVE_BROKER_STATES: frozenset[OrderState] = frozenset(
    {OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED}
)

ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.RESERVED: frozenset(
        {OrderState.VALIDATING, OrderState.NOT_SENT}
    ),
    OrderState.VALIDATING: frozenset(
        {OrderState.READY_TO_DISPATCH, OrderState.NOT_SENT}
    ),
    OrderState.READY_TO_DISPATCH: frozenset(
        {OrderState.DISPATCH_INTENT_RECORDED, OrderState.NOT_SENT}
    ),
    OrderState.DISPATCH_INTENT_RECORDED: frozenset(
        {
            OrderState.SUBMISSION_OUTCOME_UNKNOWN,
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.SUBMISSION_OUTCOME_UNKNOWN: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.RECONCILIATION_REQUIRED: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.NOT_SENT: frozenset(),
}

BROKER_STATUS_TO_STATE: dict[str, OrderState] = {
    "new": OrderState.ACCEPTED,
    "accepted": OrderState.ACCEPTED,
    "pending_new": OrderState.ACCEPTED,
    "accepted_for_bidding": OrderState.ACCEPTED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELED,
    "expired": OrderState.EXPIRED,
    # Alpaca reports done_for_day for DAY orders at the end of the session.
    # The remaining quantity will not fill; the filled quantity is preserved.
    "done_for_day": OrderState.EXPIRED,
    "rejected": OrderState.REJECTED,
    # These statuses are ambiguous for our execution contract.  Never guess.
    "pending_cancel": OrderState.RECONCILIATION_REQUIRED,
    "pending_replace": OrderState.RECONCILIATION_REQUIRED,
    "replaced": OrderState.RECONCILIATION_REQUIRED,
    "suspended": OrderState.RECONCILIATION_REQUIRED,
    "calculated": OrderState.RECONCILIATION_REQUIRED,
    "stopped": OrderState.RECONCILIATION_REQUIRED,
    "held": OrderState.RECONCILIATION_REQUIRED,
}


def is_terminal(state: OrderState | str) -> bool:
    return OrderState(state) in TERMINAL_STATES


def is_pre_dispatch(state: OrderState | str) -> bool:
    return OrderState(state) in PRE_DISPATCH_STATES


def is_post_dispatch(state: OrderState | str) -> bool:
    return OrderState(state) in POST_DISPATCH_STATES


def is_active_broker(state: OrderState | str) -> bool:
    return OrderState(state) in ACTIVE_BROKER_STATES


def can_transition(current: OrderState | str, target: OrderState | str) -> bool:
    current, target = OrderState(current), OrderState(target)
    return target in ALLOWED_TRANSITIONS[current]


def broker_state(status: str | None) -> OrderState:
    """Map a broker status string to a local factual state.

    Unknown statuses are reconciliation-required, never silently treated as
    accepted or terminal.
    """
    return BROKER_STATUS_TO_STATE.get(
        str(status or "").lower(), OrderState.RECONCILIATION_REQUIRED
    )

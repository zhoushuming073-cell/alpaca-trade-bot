"""Offline Alpaca request construction only. Never sends HTTP or changes an account."""
from .contracts import confirmed,time_state

def prepare_request(decision,*,at,last_price,held_qty=0,has_active_exit=False):
    if not time_state(at)['new_order_allowed']:raise ValueError('Window closed')
    if at>=decision.valid_until:raise ValueError('Expired decision')
    if not confirmed(decision,last_price):raise ValueError('Confirmation not met')
    if has_active_exit:raise ValueError('Cancel and reconcile existing exit before replacement')
    if decision.confirmation.kind!='NONE' and decision.confirmation.persistence=='WHILE_VALID':
        raise ValueError('Live continuing-confirmation monitoring not verified; simulation only')
    action=decision.action
    if action in {'OPEN_LONG','OPEN_SHORT'}:
        if decision.stop_price and decision.protection_tif!='day':
            raise ValueError('Native DAY bracket/OTO cannot promise independent GTC legs; choose DAY or separately reconcile later')
        if held_qty and (held_qty>0)!=(action=='OPEN_LONG'):raise ValueError('Cannot cross zero')
        payload={'symbol':decision.symbol,'qty':decision.qty,'type':'limit','limit_price':str(decision.limit_price),
                 'side':'buy' if action=='OPEN_LONG' else 'sell','time_in_force':'day','extended_hours':False,
                 'client_order_id':decision.decision_id}
        if decision.stop_price:
            payload['order_class']='bracket' if decision.take_profit else 'oto'
            payload['stop_loss']={'stop_price':str(decision.stop_price)}
            if decision.take_profit:payload['take_profit']={'limit_price':str(decision.take_profit)}
        return payload
    if action=='PROTECT':
        if not held_qty:raise ValueError('No verified existing position')
        if decision.take_profit is None:raise ValueError('Use separately validated single-stop path')
        if (held_qty>0 and decision.take_profit<=decision.stop_price) or (held_qty<0 and decision.take_profit>=decision.stop_price):raise ValueError('Linked prices inconsistent')
        return {'symbol':decision.symbol,'qty':abs(held_qty),'side':'sell' if held_qty>0 else 'buy',
                'type':'limit','order_class':'oco','time_in_force':decision.protection_tif,
                'take_profit':{'limit_price':str(decision.take_profit)},
                'stop_loss':{'stop_price':str(decision.stop_price)},'client_order_id':decision.decision_id}
    raise ValueError('No supported linked broker request for this action')

"""Optional aggressive profile: no configured values or activation without approval."""
def validate_profile(profile):
    if not profile.get('enabled'):return False
    if not profile.get('approved'):raise ValueError('Profile parameters not approved')
    if profile.get('allow_leverage'):raise ValueError('Leverage outside this version')
    limits=profile['limits']
    required=['max_single_trade_loss_fraction','max_symbol_exposure_fraction','max_gross_exposure_fraction',
              'max_daily_loss_fraction','max_drawdown_fraction','max_volume_participation']
    if any(limits.get(k) is None or not 0<float(limits[k])<=1 for k in required):raise ValueError('Explicit risk limits required')
    return True

def describe_profile(profile):
    """Expose the active policy, including the absence of numerical limits."""
    if not profile or not profile.get('enabled'):
        return {'status':'DEFAULT_PAPER','aggressive_paper_enabled':False,'approved':False,
                'limits':None,'leverage_allowed':False,'whole_shares':True,
                'note':'No aggressive-paper numerical budget is active; platform and schema constraints still apply.'}
    return {'status':'AGGRESSIVE_PAPER','aggressive_paper_enabled':True,
            'approved':bool(profile.get('approved')),'limits':dict(profile.get('limits',{})),
            'leverage_allowed':False,'whole_shares':True,
            'note':'Explicit profile limits are authoritative; no forced minimum exposure.'}

def check_order(decision,account,marks,profile,*,daily_loss=0,drawdown=0,observed_volume=None):
    if not validate_profile(profile):return
    if decision.action not in {'OPEN_LONG','OPEN_SHORT'}:return
    l=profile['limits'];equity=account['equity'];q=decision.qty;p=float(decision.limit_price)
    if decision.action=='OPEN_SHORT' and not profile.get('allow_shorting'):raise ValueError('Short permission disabled')
    if daily_loss>=l['max_daily_loss_fraction'] or drawdown>=l['max_drawdown_fraction']:raise ValueError('Risk halt: increases blocked')
    if decision.stop_price is None:raise ValueError('Explicit loss bound required in aggressive profile')
    if q*abs(p-float(decision.stop_price))>equity*l['max_single_trade_loss_fraction']:raise ValueError('Per-trade loss budget exceeded')
    held=abs(account['positions'].get(decision.symbol,0))*marks.get(decision.symbol,p)
    pending_symbol=sum(o.get('notional',0) for o in account.get('open_orders',[]) if o['symbol']==decision.symbol)
    if held+pending_symbol+q*p>equity*l['max_symbol_exposure_fraction']:raise ValueError('Concentration limit')
    pending=sum(o.get('notional',0) for o in account.get('open_orders',[]))
    if account['gross_exposure']+pending+q*p>equity*l['max_gross_exposure_fraction']:raise ValueError('Gross exposure limit')
    if observed_volume is None or q>observed_volume*l['max_volume_participation']:raise ValueError('Liquidity limit or unavailable volume')

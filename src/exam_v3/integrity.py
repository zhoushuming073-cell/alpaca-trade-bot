"""Recursive, data-based future/availability audit for one v3 input payload."""
from datetime import datetime,timezone
from .inputs import BAR_COLUMNS

def _dt(value):
    if value is None: return None
    return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(timezone.utc)

def audit_payload(payload):
    cutoff=_dt(payload['as_of_utc'])
    errors=[]; checked=0
    def check(value, path, *, allow_future=False):
        nonlocal checked
        if isinstance(value, dict):
            for key in ('first_published','updated_at','available_at','asof_utc','known_at','entry_time','start','end_at','ts','decision_time_utc','max_price_interval_end'):
                if key in value and isinstance(value[key],(str,datetime)):
                    try: stamp=_dt(value[key])
                    except Exception: continue
                    checked+=1
                    if stamp>cutoff: errors.append({'path':path+'/'+key,'value':str(stamp),'reason':'after cutoff'})
            for key,item in value.items(): check(item,path+'/'+key)
        elif isinstance(value,list):
            for i,item in enumerate(value): check(item,path+'/'+str(i))
        return
    # Account valuation must be exactly at the decision cutoff.
    account=payload.get('account',{})
    if _dt(account.get('asof_utc'))!=cutoff: errors.append({'path':'/account/asof_utc','reason':'not exact cutoff'})
    for row in payload.get('universe',[]):
        cols=row.get('bar_columns',BAR_COLUMNS)
        for series in ('daily','weekly','monthly','minutes'):
            for i,bar in enumerate(row.get(series,[])):
                if not isinstance(bar,list) or len(bar)<2: continue
                try:
                    start=_dt(bar[cols.index('start')]); end=_dt(bar[cols.index('end_at')]); avail=_dt(bar[cols.index('available_at')])
                except Exception: continue
                checked+=2
                if end<=start: errors.append({'path':f"/universe/{row.get('symbol')}/{series}/{i}",'reason':'end<=start'})
                if end>cutoff: errors.append({'path':f"/universe/{row.get('symbol')}/{series}/{i}/end_at",'reason':'after cutoff'})
                if avail>cutoff: errors.append({'path':f"/universe/{row.get('symbol')}/{series}/{i}/available_at",'reason':'after cutoff'})
                if avail<end: errors.append({'path':f"/universe/{row.get('symbol')}/{series}/{i}/available_at",'reason':'available before end'})
        for key in ('news','financials'):
            check(row.get(key,[]),f"/universe/{row.get('symbol')}/{key}")
    for key in ('position_theses','profit_context','risk_policy','time_state'):
        check(payload.get(key,{}),f'/{key}')
    return {'ok':not errors,'checked':checked,'errors':errors,'cutoff':cutoff.isoformat()}


def audit_source(payload, conn):
    """Compare packed bars, news and facts with the source DuckDB row-by-row."""
    errors=[]; checked=0
    for row in payload.get('universe',[]):
        symbol=row.get('symbol'); cols=row.get('bar_columns',BAR_COLUMNS)
        for series,tf in (('daily','1Day'),('minutes','1Min')):
            for i,bar in enumerate(row.get(series,[])):
                if not isinstance(bar,list): continue
                start=bar[cols.index('start')]; end=bar[cols.index('end_at')]; avail=bar[cols.index('available_at')]
                db=conn.execute("SELECT end_at,available_at,open,high,low,close,volume FROM bars WHERE symbol=? AND timeframe=? AND feed='iex' AND ts=?",[symbol,tf,start]).fetchone()
                checked+=1
                if not db:
                    errors.append({'path':f'/{symbol}/{series}/{i}','reason':'source row missing'}); continue
                input_row=[end,avail,bar[cols.index('open')],bar[cols.index('high')],bar[cols.index('low')],bar[cols.index('close')],bar[cols.index('volume')]]
                if str(db[0])!=str(input_row[0]) or str(db[1])!=str(input_row[1]) or any(float(a)!=float(b) for a,b in zip(db[2:],input_row[2:])):
                    errors.append({'path':f'/{symbol}/{series}/{i}','reason':'source mismatch','source':[str(x) for x in db[:2]]+[float(x) for x in db[2:]],'input':input_row})
        for item in row.get('news',[]):
            db=conn.execute("SELECT first_published,title FROM evidence WHERE id=?",[item.get('id')]).fetchone(); checked+=1
            if not db or str(db[0])!=str(item.get('first_published')) or str(db[1])!=str(item.get('title')):
                errors.append({'path':f'/{symbol}/news/{item.get("id")}','reason':'news source mismatch'})
        for item in row.get('financials',[]):
            rows=conn.execute("SELECT value FROM fundamentals WHERE symbol=? AND tag=? AND period_end=? AND available_at=?",
                              [symbol,item.get('tag'),item.get('period_end'),item.get('available_at')]).fetchall(); checked+=1
            if not any(float(x[0])==float(item.get('value')) for x in rows):
                errors.append({'path':f'/{symbol}/financials/{item.get("tag")}','reason':'fact source mismatch'})
        quote=row.get('quote')
        if quote:
            db=conn.execute("SELECT close FROM bars WHERE symbol=? AND timeframe='1Min' AND feed='iex' AND ts=?",[symbol,quote.get('ts')]).fetchone(); checked+=1
            if not db or float(db[0])!=float(quote.get('last')):
                errors.append({'path':f'/{symbol}/quote','reason':'quote source mismatch'})
    return {'ok':not errors,'checked':checked,'errors':errors}

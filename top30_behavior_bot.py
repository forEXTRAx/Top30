import json, os, time
from pathlib import Path
import requests

API='https://api.dexscreener.com'
CHAIN='solana'  # only Solana tokens are tracked/scored
TOP_N=30
CAND='top30_candidates.json'      # tokens not yet in top30 (pruned after 3 days if never promoted)
DATA='top30_snapshots.json'       # PERMANENT dataset: every token that ever reached top30, never pruned
STATE='top30_state.json'
SNAP_DIR='snapshots'              # one detailed JSON file per token, sent to Telegram at entry moment
MAX_AGE_DAYS=3
MAX_CANDIDATES=250  # hard cap on the not-yet-promoted pool to stay within API rate limits
# Checkpoints (minutes since launch) captured while a token is still pre-entry.
# Dense in the first hour (when most behavioral signal shows up), sparser after.
CHECKPOINTS_MIN=[1,2,3,5,10,15,20,30,45,60,90,120,180,240,360,480,720,1440,2160,2880,4320]
TG_TOKEN=os.environ.get('TELEGRAM_BOT_TOKEN')
TG_CHAT=os.environ.get('TELEGRAM_CHAT_ID')


def load(p,d):
    try: return json.loads(Path(p).read_text(encoding='utf-8'))
    except Exception: return d

def save(p,d): Path(p).write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')

def ms(): return int(time.time()*1000)

def num(x):
    try: return float(x)
    except Exception: return None

def pct(a,b): return None if a in (None,0) or b is None else (b/a-1)*100

def get(url,retries=3):
    for attempt in range(retries):
        try:
            r=requests.get(url,timeout=20,headers={'User-Agent':'Top30BehaviorBot/1.0'})
            if r.status_code==429:
                time.sleep(1.5*(attempt+1)); continue
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as e:
            if attempt==retries-1: raise
            time.sleep(1.5*(attempt+1))
    r.raise_for_status()
    return r

def token_pairs(addr):
    try:
        pairs=get(f'{API}/latest/dex/tokens/{addr}').json().get('pairs') or []
        time.sleep(0.12)  # gentle pacing to avoid hitting the API rate limit
        return pairs
    except Exception as e: print('pair error',e); return []

def best_pair(pairs):
    return max(pairs,key=lambda p:(num((p.get('liquidity') or {}).get('usd')) or 0,num((p.get('volume') or {}).get('h24')) or 0),default=None)

def snapshot(pair, rank=None, launch_ms=None):
    tx=pair.get('txns') or {}; pc=pair.get('priceChange') or {}; vol=pair.get('volume') or {}; liq=pair.get('liquidity') or {}
    now=ms()
    s={
      'timestamp_ms':now,'age_min':max(0,(now-(launch_ms or pair.get('pairCreatedAt') or now))/60000),
      'top30_rank':rank,'chain':pair.get('chainId'),'dex':pair.get('dexId'),'pair_address':pair.get('pairAddress'),
      'pair_created_at_ms':pair.get('pairCreatedAt'),'price_usd':num(pair.get('priceUsd')),'price_native':num(pair.get('priceNative')),
      'market_cap_usd':num(pair.get('marketCap')),'fdv_usd':num(pair.get('fdv')),
      'liquidity_usd':num(liq.get('usd')),'liquidity_base':num(liq.get('base')),'liquidity_quote':num(liq.get('quote')),
      'volume_m5_usd':num(vol.get('m5')),'volume_h1_usd':num(vol.get('h1')),'volume_h6_usd':num(vol.get('h6')),'volume_h24_usd':num(vol.get('h24')),
      'buys_m5':num((tx.get('m5') or {}).get('buys')),'sells_m5':num((tx.get('m5') or {}).get('sells')),
      'buys_h1':num((tx.get('h1') or {}).get('buys')),'sells_h1':num((tx.get('h1') or {}).get('sells')),
      'buys_h6':num((tx.get('h6') or {}).get('buys')),'sells_h6':num((tx.get('h6') or {}).get('sells')),
      'buys_h24':num((tx.get('h24') or {}).get('buys')),'sells_h24':num((tx.get('h24') or {}).get('sells')),
      'price_change_5m_pct':num(pc.get('m5')),'price_change_1h_pct':num(pc.get('h1')),'price_change_6h_pct':num(pc.get('h6')),'price_change_24h_pct':num(pc.get('h24')),
      'url':pair.get('url'),'labels':pair.get('labels') or [],'info':pair.get('info') or {},'base_token':pair.get('baseToken') or {},
    }
    buys5=s['buys_m5'] or 0; sells5=s['sells_m5'] or 0
    buys1=s['buys_h1'] or 0; sells1=s['sells_h1'] or 0
    liq_usd=s['liquidity_usd'] or 0
    s['buy_sell_ratio_m5']=None if sells5==0 else buys5/sells5
    s['buy_sell_ratio_h1']=None if sells1==0 else buys1/sells1
    s['net_buys_m5']=buys5-sells5
    s['net_buys_h1']=buys1-sells1
    s['volume_liquidity_ratio_h1']=None if not liq_usd else (s['volume_h1_usd'] or 0)/liq_usd
    s['volume_liquidity_ratio_m5']=None if not liq_usd else (s['volume_m5_usd'] or 0)/liq_usd
    # Capital-inflow proxy: how much money is moving in relative to pool depth,
    # weighted toward buy dominance. Higher = stronger, more aggressive net buying.
    s['capital_inflow_index']=None if not liq_usd else round(((s['volume_m5_usd'] or 0)*((buys5+1)/(sells5+1)))/liq_usd,4)
    s['capital_inflow_index_h1']=None if not liq_usd else round(((s['volume_h1_usd'] or 0)*((buys1+1)/(sells1+1)))/liq_usd,4)
    return s

def trend_score(pair):
    # Heuristic "trending" score built entirely from official API fields, since
    # DexScreener's own homepage ranking algorithm is not publicly documented or
    # exposed via API (that page is client-rendered and Cloudflare-protected).
    # Blended 1h + 6h windows, weighted toward 6h per current tuning — this is our
    # best proxy for the site's "Trending 6H" tab, not an exact match.
    vol=pair.get('volume') or {}; tx=pair.get('txns') or {}; liq=pair.get('liquidity') or {}; pc=pair.get('priceChange') or {}
    liquidity_usd=num(liq.get('usd')) or 0
    vol_h1=num(vol.get('h1')) or 0; vol_h6=num(vol.get('h6')) or 0
    buys_h1=num((tx.get('h1') or {}).get('buys')) or 0; sells_h1=num((tx.get('h1') or {}).get('sells')) or 0
    buys_h6=num((tx.get('h6') or {}).get('buys')) or 0; sells_h6=num((tx.get('h6') or {}).get('sells')) or 0
    vol_liq_ratio_h1=(vol_h1/liquidity_usd) if liquidity_usd else 0
    vol_liq_ratio_h6=(vol_h6/liquidity_usd) if liquidity_usd else 0
    momentum_h1=max(num(pc.get('h1')) or 0,0)
    momentum_h6=max(num(pc.get('h6')) or 0,0)
    score_h1=vol_h1 + (buys_h1+sells_h1)*10 + vol_liq_ratio_h1*300 + momentum_h1*5
    score_h6=vol_h6 + (buys_h6+sells_h6)*10 + vol_liq_ratio_h6*300 + momentum_h6*5
    return score_h1*1 + score_h6*2  # 6h weighted 2x heavier than 1h

def boosted_tokens():
    # DexScreener has no public endpoint for its homepage "trending" ranking (it's
    # rendered client-side and blocks non-browser requests with 403). Boosted tokens
    # are an official, API-exposed attention signal used to widen the candidate pool.
    # Returns boost metadata too, so boosted-sourced tokens can be flagged with their
    # boost coefficient rather than silently blended in as if organically discovered.
    out=[]
    for ep in ('/token-boosts/latest/v1','/token-boosts/top/v1'):
        try: j=get(f'{API}{ep}').json()
        except Exception as e: print('boost error',ep,e); j=[]
        for x in j if isinstance(j,list) else []:
            chain=x.get('chainId'); addr=x.get('tokenAddress')
            if chain and addr:
                out.append({'chain':chain,'address':addr,
                            'boost_amount':num(x.get('amount')),
                            'boost_total_amount':num(x.get('totalAmount'))})
    return out

def apply_boost_info(rec,binfo,now_ms):
    # Marks a record (candidate or permanent) as boosted and records the boost
    # coefficient. Called both when a boosted token is first discovered and when
    # an already-tracked token later receives a boost, so the signal is never lost.
    rec['is_boosted']=True
    rec['boost_amount']=binfo.get('boost_amount')
    rec['boost_total_amount']=binfo.get('boost_total_amount')
    rec.setdefault('boost_events',[]).append({'ts_ms':now_ms,'amount':binfo.get('boost_amount'),
                                               'total_amount':binfo.get('boost_total_amount')})

def tg_send_message(text):
    if not (TG_TOKEN and TG_CHAT): print('telegram skipped: no token/chat configured'); return
    try: requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                        data={'chat_id':TG_CHAT,'text':text,'parse_mode':'HTML','disable_web_page_preview':True},timeout=20)
    except Exception as e: print('telegram message error',e)

def tg_send_document(path,caption):
    if not (TG_TOKEN and TG_CHAT): return
    try:
        with open(path,'rb') as f:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendDocument',
                          data={'chat_id':TG_CHAT,'caption':caption[:1024]},files={'document':f},timeout=30)
    except Exception as e: print('telegram document error',e)

def safe_name(s):
    s=''.join(ch for ch in (s or '') if ch.isalnum() or ch in ('-','_'))
    return s[:32] or 'TOKEN'

def fmt_num(x,d=2):
    if x is None: return 'n/a'
    a=abs(x)
    if a>=1_000_000: return f'{x/1_000_000:.{d}f}M'
    if a>=1_000: return f'{x/1_000:.{d}f}K'
    return f'{x:.{d}f}'

def entry_alert(chain,address,rec,checkpoints):
    s=rec['entry_snapshot']; bt=s.get('base_token') or {}
    sym=bt.get('symbol') or '?'; name=bt.get('name') or ''
    boost_line=(f"Boosted: yes (amount {fmt_num(rec.get('boost_amount'))}/{fmt_num(rec.get('boost_total_amount'))})\n"
                if rec.get('is_boosted') else f"Boosted: no (organic, {rec.get('discovery_source','organic')})\n")
    msg=(f"🚀 <b>{sym}</b> ({name}) entered Top 30 — rank #{rec['top30_entry_rank']}\n"
         f"Chain: {chain} | Dex: {s.get('dex')}\n"
         f"{boost_line}"
         f"Age at entry: {fmt_num(s.get('age_min'),1)} min\n"
         f"Price: ${fmt_num(s.get('price_usd'),6)}\n"
         f"MCap: ${fmt_num(s.get('market_cap_usd'))} | Liq: ${fmt_num(s.get('liquidity_usd'))}\n"
         f"Vol 5m/1h: ${fmt_num(s.get('volume_m5_usd'))}/${fmt_num(s.get('volume_h1_usd'))}\n"
         f"Buy/Sell 5m: {s.get('buys_m5')}/{s.get('sells_m5')} (ratio {fmt_num(s.get('buy_sell_ratio_m5'))})\n"
         f"Capital inflow idx (5m/1h): {fmt_num(s.get('capital_inflow_index'))}/{fmt_num(s.get('capital_inflow_index_h1'))}\n"
         f"Chg 5m/1h/24h: {fmt_num(s.get('price_change_5m_pct'))}%/{fmt_num(s.get('price_change_1h_pct'))}%/{fmt_num(s.get('price_change_24h_pct'))}%\n"
         f"{s.get('url') or ''}")
    tg_send_message(msg)
    Path(SNAP_DIR).mkdir(exist_ok=True)
    fpath=f"{SNAP_DIR}/{safe_name(chain)}_{safe_name(sym)}_{address[:8]}.json"
    save(fpath,{'token_id':rec['token_id'],'chain':chain,'address':address,'symbol':sym,'name':name,
                'launch_time_ms':rec['launch_time_ms'],'top30_entry_time_ms':rec['top30_entry_time_ms'],
                'top30_entry_rank':rec['top30_entry_rank'],'pre_entry_checkpoints':checkpoints,
                'entry_snapshot':rec['entry_snapshot']})
    tg_send_document(fpath,f"Pre-entry behavioral snapshot for {sym} ({chain}) — entry rank #{rec['top30_entry_rank']}")

def update_checkpoints(cand,snap):
    age=snap['age_min']; cps=cand.setdefault('checkpoints',{})
    for m in CHECKPOINTS_MIN:
        key=str(m)
        if age>=m and key not in cps:
            cps[key]=snap

def main():
    candidates=load(CAND,{'version':1,'tokens':{}})
    data=load(DATA,{'version':1,'tokens':{}})
    state=load(STATE,{'version':1})
    now_ms=ms()

    # One-time (idempotent) cleanup: this bot now tracks Solana only. Purge any
    # non-Solana records left over from before this filter existed.
    for tid in [t for t,c in candidates['tokens'].items() if c.get('chain')!=CHAIN]:
        del candidates['tokens'][tid]
    for tid in [t for t,r in data['tokens'].items() if r.get('chain')!=CHAIN]:
        del data['tokens'][tid]

    # 1) Discover newly launched tokens and start tracking them from as close to
    #    launch as possible.
    try: profiles=get(f'{API}/token-profiles/latest/v1').json()
    except Exception as e: print('profile error',e); profiles=[]
    for x in profiles if isinstance(profiles,list) else []:
        if len(candidates['tokens'])>=MAX_CANDIDATES: break
        chain=x.get('chainId'); addr=x.get('tokenAddress')
        if chain!=CHAIN or not addr: continue
        tid=f'{chain}:{addr}'
        if tid in data['tokens'] or tid in candidates['tokens']: continue
        pairs=[p for p in token_pairs(addr) if p.get('chainId')==chain]
        pair=best_pair(pairs)
        if not pair: continue
        launch=pair.get('pairCreatedAt') or now_ms
        age=(now_ms-launch)/60000
        if age<0 or age>5: continue  # only pick up tokens still in their first 0-5 min of life
        candidates['tokens'][tid]={'id':tid,'chain':chain,'address':addr,'pair_address':pair.get('pairAddress'),
                                    'launch_time_ms':launch,'checkpoints':{},'discovery_source':'organic'}
        update_checkpoints(candidates['tokens'][tid],snapshot(pair,launch_ms=launch))

    # 2) Widen the pool with currently-boosted tokens (an official attention signal).
    # These are NOT held to the 0-5min organic window (boosts can land at any token
    # age), so any record sourced this way is explicitly flagged as boosted with its
    # coefficient — it will not have full from-launch checkpoint history like organic
    # entries do. If the token is already tracked (candidate or permanent), we still
    # attach/update the boost info instead of skipping it.
    for binfo in boosted_tokens():
        chain,addr=binfo['chain'],binfo['address']
        if chain!=CHAIN: continue
        tid=f'{chain}:{addr}'
        if tid in data['tokens']:
            apply_boost_info(data['tokens'][tid],binfo,now_ms); continue
        if tid in candidates['tokens']:
            apply_boost_info(candidates['tokens'][tid],binfo,now_ms); continue
        if len(candidates['tokens'])>=MAX_CANDIDATES: continue
        pairs=[p for p in token_pairs(addr) if p.get('chainId')==chain]
        pair=best_pair(pairs)
        if not pair: continue
        launch=pair.get('pairCreatedAt') or now_ms
        if (now_ms-launch)/86400000>MAX_AGE_DAYS: continue  # already stale, skip add-then-prune churn
        rec={'id':tid,'chain':chain,'address':addr,'pair_address':pair.get('pairAddress'),
             'launch_time_ms':launch,'checkpoints':{},'discovery_source':'boosted'}
        apply_boost_info(rec,binfo,now_ms)
        candidates['tokens'][tid]=rec
        update_checkpoints(rec,snapshot(pair,launch_ms=launch))

    # 3) Refresh a live snapshot for every token still in play: not-yet-promoted
    #    candidates, plus already-promoted tokens still active in top30 (needed to
    #    keep updating rank/peak/exit). Tokens that already exited top30 are frozen
    #    permanent records and are not re-fetched.
    live=[]  # (tid, chain, address, pair_address_hint, pair_json, snap)
    for tid,c in candidates['tokens'].items():
        pairs=token_pairs(c['address'])
        pair=next((p for p in pairs if p.get('pairAddress')==c.get('pair_address')),None) or best_pair(pairs)
        if not pair: continue
        c['pair_address']=pair.get('pairAddress')
        snap=snapshot(pair,launch_ms=c['launch_time_ms'])
        update_checkpoints(c,snap)
        live.append((tid,c['chain'],c['address'],pair,snap))
    for tid,rec in data['tokens'].items():
        if rec.get('top30_exit_time_ms') is not None: continue  # frozen, skip
        pairs=token_pairs(rec['address'])
        pair=next((p for p in pairs if p.get('pairAddress')==rec.get('pair_address')),None) or best_pair(pairs)
        if not pair: continue
        rec['pair_address']=pair.get('pairAddress')
        snap=snapshot(pair,launch_ms=rec['launch_time_ms'])
        live.append((tid,rec['chain'],rec['address'],pair,snap))

    # 4) Rank everything currently live by our trend score; take the top 30.
    ranked=sorted(live,key=lambda t:trend_score(t[3]),reverse=True)[:TOP_N]
    rank={tid:i+1 for i,(tid,*_ ) in enumerate(ranked)}
    top_ids=set(rank.keys())

    promoted=0
    for tid,chain,address,pair,snap in ranked:
        r=rank[tid]
        snap['top30_rank']=r
        rec=data['tokens'].get(tid)
        if rec is None:
            # First time ever reaching top30 -> permanent record created, entry alert fired once.
            cand=candidates['tokens'].get(tid,{})
            rec={'token_id':tid,'chain':chain,'address':address,'pair_address':pair.get('pairAddress'),
                 'launch_time_ms':cand.get('launch_time_ms',snap['timestamp_ms']-snap['age_min']*60000),
                 'discovery_source':cand.get('discovery_source','organic'),
                 'is_boosted':cand.get('is_boosted',False),'boost_amount':cand.get('boost_amount'),
                 'boost_total_amount':cand.get('boost_total_amount'),'boost_events':cand.get('boost_events',[]),
                 'top30_entry_time_ms':now_ms,'top30_entry_rank':r,'entry_snapshot':snap,
                 'top30_peak_rank':r,'top30_peak_rank_time_ms':now_ms,'top30_exit_time_ms':None,'top30_duration_seconds':None,
                 'rank_history':[],'peak_metrics':{},'behavior_summary':{}}
            data['tokens'][tid]=rec; promoted+=1
            entry_alert(chain,address,rec,cand.get('checkpoints',{}))
            candidates['tokens'].pop(tid,None)  # now permanently tracked in DATA instead
        rec['rank_history'].append({'timestamp_ms':now_ms,'rank':r,'snapshot':snap})
        if r<rec['top30_peak_rank']:
            rec['top30_peak_rank']=r; rec['top30_peak_rank_time_ms']=now_ms
        prices=[h.get('snapshot',{}).get('price_usd') for h in rec['rank_history'] if h.get('snapshot',{}).get('price_usd') is not None]
        if prices:
            peak=max(prices)
            peak_hit=next((h for h in rec['rank_history'] if h.get('snapshot',{}).get('price_usd')==peak),None)
            entry_price=rec['entry_snapshot'].get('price_usd')
            rec['peak_metrics']={'peak_price_usd':peak,'peak_gain_from_entry_pct':pct(entry_price,peak),
                                  'peak_time_ms':peak_hit['timestamp_ms'] if peak_hit else None}

    # 5) Anything that was active and just fell out of top30 gets frozen (exit time
    #    set) but stays in DATA forever as a completed behavioral record.
    for tid,rec in data['tokens'].items():
        if rec.get('top30_exit_time_ms') is None and tid not in top_ids and tid in {t for t,*_ in live}:
            rec['top30_exit_time_ms']=now_ms
            rec['top30_duration_seconds']=(now_ms-rec['top30_entry_time_ms'])/1000
            rec['behavior_summary']={
                'time_launch_to_entry_min':(rec['top30_entry_time_ms']-rec['launch_time_ms'])/60000,
                'time_entry_to_peak_min':(rec['top30_peak_rank_time_ms']-rec['top30_entry_time_ms'])/60000,
                'time_in_top30_min':rec['top30_duration_seconds']/60,
            }

    # 6) Prune only from the *candidates* pool (never-promoted tokens) once 3 days
    #    have passed since launch, to keep the working set light. Promoted tokens in
    #    DATA are never pruned — they are the permanent success dataset.
    for tid in list(candidates['tokens'].keys()):
        c=candidates['tokens'][tid]
        age_d=(now_ms-c.get('launch_time_ms',now_ms))/86400000
        if age_d>MAX_AGE_DAYS:
            del candidates['tokens'][tid]

    state.update({'last_run_ms':now_ms,'top30_count':len(ranked),'candidates_tracked':len(candidates['tokens']),
                  'permanent_dataset_size':len(data['tokens']),'promoted_this_run':promoted})
    save(CAND,candidates); save(DATA,data); save(STATE,state)
    print('Top30:',len(ranked),'permanent dataset:',len(data['tokens']),'candidates tracked:',len(candidates['tokens']),'promoted this run:',promoted)

if __name__=='__main__': main()

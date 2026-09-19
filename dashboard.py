"""Live browser dashboard for prices, Jev decisions, setup rules, and execution.

The server only trusts loopback ``Host`` headers, and the kill switch endpoint
requires a custom request header that a cross-site page cannot send without a
CORS preflight this server never approves.
"""

from typing import Callable, Optional

from flask import Flask, jsonify, render_template_string, request

from executor import ExecutionState
from signals import BUY_SIGNALS, SELL_SIGNALS, Setup
from state import LiveQuote, SignalStore, Snapshot


TRUSTED_HOSTS = ["127.0.0.1", "localhost"]
FLATTEN_HEADER = "X-Requested-With"
FLATTEN_HEADER_VALUE = "moomoo-jev-trader"

DASHBOARD = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{{ symbol }} · Jev live trader</title>
  <style>
    :root {
      color-scheme:light; --page:#efede8; --paper:#fff; --panel:#f7f6f2; --line:#e8e6df;
      --ink:#11110f; --muted:#77756d; --buy:#119d63; --buy-soft:#d9eee3;
      --sell:#dc563f; --sell-soft:#f8ddd6; --flat:#c18a26; --purple:#6857d9;
    }
    *{box-sizing:border-box} body{margin:0;background:var(--page);color:var(--ink);font:14px Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}
    button{font:inherit} .shell{width:min(1440px,calc(100% - 28px));min-height:calc(100vh - 28px);margin:14px auto;background:var(--paper);border:1px solid var(--line);border-radius:18px;overflow:hidden}
    header{height:72px;padding:0 22px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);gap:16px}
    .brand{display:flex;align-items:center;gap:12px}.brand h1{font-size:18px;margin:0}.brand small,.muted{color:var(--muted)}
    .live{display:inline-flex;align-items:center;gap:7px;font-weight:650}.dot{width:8px;height:8px;border-radius:50%;background:#42c879;box-shadow:0 0 0 4px #dff5e8}.dot.off{background:var(--sell);box-shadow:none}
    .badges{display:flex;gap:8px;align-items:center}.badge{padding:5px 9px;border-radius:999px;background:#ede9ff;color:#5141b5;font:600 12px ui-monospace,monospace}.badge.mode{background:#f7ead1;color:#895d10}
    .stats{display:grid;grid-template-columns:repeat(6,1fr);border-bottom:1px solid var(--line)}.stat{padding:12px 18px;border-right:1px solid var(--line)}.stat:last-child{border:0}.k{font-size:10px;letter-spacing:.11em;color:var(--muted);text-transform:uppercase}.v{font:650 18px ui-monospace,SFMono-Regular,monospace;margin-top:4px;font-variant-numeric:tabular-nums}
    .main{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(360px,.75fr);min-height:520px;border-bottom:1px solid var(--line)}
    .chartPanel{position:relative;min-height:520px;border-right:1px solid var(--line);overflow:hidden;background:linear-gradient(#fff,#fdfcf9)}
    .chartTop{position:absolute;z-index:2;left:22px;top:19px}.price{font:650 34px ui-monospace,monospace;letter-spacing:-.04em}.priceSub{display:flex;gap:14px;color:var(--muted);margin-top:3px;font:12px ui-monospace,monospace}
    #chart{position:absolute;inset:0;width:100%;height:100%}.grid{stroke:#edebe6;stroke-width:1}.axis{fill:#8c8980;font:11px ui-monospace,monospace}.priceLine{fill:none;stroke:#171713;stroke-width:1.25;opacity:.32}.area{fill:url(#fade)}.marker{stroke:#fff;stroke-width:2}.wick{stroke-width:1}.candleUp{fill:var(--buy);stroke:var(--buy)}.candleDown{fill:var(--sell);stroke:var(--sell)}.volumeUp{fill:var(--buy-soft)}.volumeDown{fill:var(--sell-soft)}
    .strip{position:absolute;left:22px;right:22px;bottom:18px;height:18px;display:flex;gap:3px;justify-content:flex-end}.tick{width:6px;height:18px;border-radius:3px;background:#ddd}.tick.up{background:var(--buy)}.tick.down{background:var(--sell)}.tick.sideways{background:#d6a74d}
    .right{display:flex;flex-direction:column;min-width:0}.decision{padding:20px;border-bottom:1px solid var(--line)}.eyebrow{font-size:10px;letter-spacing:.12em;color:var(--muted);text-transform:uppercase}.call{display:flex;align-items:baseline;gap:12px;margin:9px 0 15px}.callWord{font-size:31px;font-weight:700}.callPct{font:600 20px ui-monospace,monospace}
    .barRow{display:grid;grid-template-columns:72px 1fr 44px;gap:10px;align-items:center;margin:8px 0}.barLabel{font-size:13px}.track{height:15px;border-radius:99px;background:#f0eee9;overflow:hidden}.fill{height:100%;border-radius:99px;transition:width .3s}.pct{text-align:right;font:600 13px ui-monospace,monospace}
    .reviewMeta{display:flex;justify-content:space-between;color:var(--muted);font:11px ui-monospace,monospace;margin-top:14px}.quality{padding:15px 20px;border-bottom:1px solid var(--line);font-size:13px}.quality strong{font-weight:650}.risk{color:var(--sell)}
    .feed{padding:16px 20px;min-height:0;flex:1}.feedHead{display:flex;justify-content:space-between;margin-bottom:8px}.feedRows{font:11px ui-monospace,SFMono-Regular,monospace}.feedRow{height:25px;display:grid;grid-template-columns:58px 46px 52px 1fr 55px;gap:8px;align-items:center;border-top:1px solid #f0eee9;animation:in .25s ease}.feedRow:first-child{font-weight:650}.wordUp{color:var(--buy)}.wordDown{color:var(--sell)}.wordSideways{color:var(--flat)}@keyframes in{from{opacity:.2;transform:translateY(-3px)}}
    .lower{display:grid;grid-template-columns:1fr 1fr 1.25fr;min-height:210px}.pane{padding:18px 20px;border-right:1px solid var(--line)}.pane:last-child{border:0}.checks{list-style:none;margin:10px 0 0;padding:0}.checks li{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #f0eee9}.yes{color:var(--buy)}.no{color:var(--sell)}
    .setupCall{font-size:22px;font-weight:700;margin:8px 0}.detail{color:var(--muted);line-height:1.55}.execLine{margin-top:10px;padding:12px;background:var(--panel);border-radius:10px}.kill{margin-top:12px;border:0;border-radius:8px;padding:8px 12px;background:var(--sell);color:white;font-weight:700;cursor:pointer}
    #error{display:none;background:#fff0ed;color:#a92f20;padding:10px 20px;border-bottom:1px solid #f5cdc5}
    @media(max-width:950px){.stats{grid-template-columns:repeat(3,1fr)}.main{grid-template-columns:1fr}.chartPanel{border-right:0;border-bottom:1px solid var(--line);min-height:430px}.lower{grid-template-columns:1fr}.pane{border-right:0;border-bottom:1px solid var(--line)}}
    @media(max-width:600px){.shell{width:100%;margin:0;border-radius:0;border:0}.badges{display:none}.stats{grid-template-columns:repeat(2,1fr)}.main{display:block}.chartPanel{min-height:360px}.feedRow{grid-template-columns:54px 42px 46px 1fr}.feedRow span:last-child{display:none}}
  </style>
</head>
<body>
<div class="shell">
  <header>
    <div class="brand"><div><h1>{{ symbol }} · Jev live trader</h1><small>one-minute setup · {{ benchmark }} benchmark</small></div></div>
    <div class="badges"><span id="model" class="badge">jev</span><span id="mode" class="badge mode">shadow</span><span class="live"><i id="dot" class="dot off"></i><span id="connection">connecting</span></span></div>
  </header>
  <div id="error"></div>
  <section class="stats">
    <div class="stat"><div class="k">Live price</div><div id="s-price" class="v">—</div></div>
    <div class="stat"><div class="k">Jev direction</div><div id="s-direction" class="v">—</div></div>
    <div class="stat"><div class="k">Up probability</div><div id="s-up" class="v">—</div></div>
    <div class="stat"><div class="k">Last latency</div><div id="s-latency" class="v">—</div></div>
    <div class="stat"><div class="k">Jev calls</div><div id="s-calls" class="v">0</div></div>
    <div class="stat"><div class="k">Trades today</div><div id="s-trades" class="v">0</div></div>
  </section>
  <section class="main">
    <div class="chartPanel">
      <div class="chartTop"><div id="hero-price" class="price">—</div><div class="priceSub"><span>{{ symbol }} · REAL 1M OHLC</span><span id="hero-change">waiting for Moomoo bars</span><span id="hero-time"></span></div></div>
      <svg id="chart" preserveAspectRatio="none"><defs><linearGradient id="fade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#111" stop-opacity=".07"/><stop offset="1" stop-color="#111" stop-opacity="0"/></linearGradient></defs><g id="chart-grid"></g><g id="chart-volume"></g><path id="chart-area" class="area"/><path id="chart-line" class="priceLine"/><g id="chart-candles"></g><g id="chart-marks"></g><g id="chart-axis"></g></svg>
      <div id="strip" class="strip"></div>
    </div>
    <aside class="right">
      <div class="decision">
        <div class="eyebrow">Jev · next five minutes</div>
        <div class="call"><span id="call-word" class="callWord">WAITING</span><span id="call-pct" class="callPct"></span></div>
        <div id="prob-bars"></div>
        <div class="reviewMeta"><span id="review-time">no review yet</span><span id="review-latency"></span></div>
      </div>
      <div class="quality"><strong id="quality">Entry quality —</strong><div id="stop-risk" class="detail">Stop-first risk —</div></div>
      <div class="feed"><div class="feedHead"><span class="eyebrow">Live Jev feed</span><span class="muted">5 second cadence</span></div><div id="feed" class="feedRows"></div></div>
    </aside>
  </section>
  <section class="lower">
    <div class="pane"><div class="eyebrow">Buy checks</div><ul id="buy-checks" class="checks"></ul></div>
    <div class="pane"><div class="eyebrow">Sell checks</div><ul id="sell-checks" class="checks"></ul></div>
    <div class="pane"><div class="eyebrow">Rule engine & execution</div><div id="setup-call" class="setupCall">Waiting</div><div id="setup-detail" class="detail"></div><div id="execution" class="execLine">Auto trading is not running</div><button id="kill" class="kill" style="display:none">Flatten and stop</button></div>
  </section>
</div>
<script>
let setupData=null, liveData=null;
const errors={setup:null,live:null,quote:null};
const $=id=>document.getElementById(id), pct=n=>Number.isFinite(n)?Math.round(n*100)+'%':'—', money=n=>Number.isFinite(n)?'$'+n.toFixed(2):'—';
function text(id,value){$(id).textContent=value}
function showErrors(){const msg=errors.setup||errors.live||errors.quote;text('error',msg||'');$('error').style.display=msg?'block':'none'}
function directionClass(d){return d==='up'?'wordUp':d==='down'?'wordDown':'wordSideways'}
function bars(review){
  const probs=review?.direction?.probabilities||{up:0,sideways:0,down:0}; const colors={up:'var(--buy)',sideways:'#d5a342',down:'var(--sell)'};
  $('prob-bars').innerHTML=''; ['up','sideways','down'].forEach(k=>{const row=document.createElement('div');row.className='barRow';const label=document.createElement('span');label.className='barLabel';label.textContent=k;const track=document.createElement('div');track.className='track';const fill=document.createElement('div');fill.className='fill';fill.style.width=((probs[k]||0)*100)+'%';fill.style.background=colors[k];track.append(fill);const value=document.createElement('span');value.className='pct';value.textContent=pct(probs[k]);row.append(label,track,value);$('prob-bars').append(row)});
}
function renderReview(r){
  if(!r||r.status!=='complete'){text('call-word','WAITING');text('call-pct','');bars(null);text('quality','Entry quality —');text('stop-risk',r?.summary||'Waiting for Jev');return}
  const d=r.direction?.choice||'sideways', p=r.direction?.probabilities?.[d]||0, up=r.direction?.probabilities?.up||0;
  text('call-word',d.toUpperCase());$('call-word').className='callWord '+directionClass(d);text('call-pct',pct(p));bars(r);
  text('quality','Entry quality '+(r.entry_quality?.choice||'—'));text('stop-risk','Stop-first risk '+pct(r.stop_first));$('stop-risk').className='detail '+(r.stop_first>.45?'risk':'');
  text('review-time',new Date(r.evaluated_at).toLocaleTimeString());text('review-latency',(r.latency_ms||0)+' ms');text('model',r.model||'jev');
  text('s-direction',d.toUpperCase());$('s-direction').className='v '+directionClass(d);text('s-up',pct(up));text('s-latency',(r.latency_ms||0)+'ms');
}
function renderFeed(reviews){
  const list=$('feed');list.innerHTML='';reviews.slice(-16).reverse().forEach(r=>{const d=r.direction?.choice||'sideways',up=r.direction?.probabilities?.up||0,q=r.entry_quality?.choice||'—';const row=document.createElement('div');row.className='feedRow';[new Date(r.evaluated_at).toLocaleTimeString([], {hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}),d.toUpperCase(),pct(up),q,(r.latency_ms||0)+'ms'].forEach((v,i)=>{const s=document.createElement('span');s.textContent=v;if(i===1)s.className=directionClass(d);row.append(s)});list.append(row)})}
function renderStrip(reviews){const el=$('strip');el.innerHTML='';reviews.slice(-90).forEach(r=>{const i=document.createElement('i');i.className='tick '+(r.direction?.choice||'sideways');i.title=new Date(r.evaluated_at).toLocaleTimeString()+' '+(r.direction?.choice||'');el.append(i)})}
function renderChart(bars,reviews,latestQuote){
  if(!bars?.length)return;const svg=$('chart'),box=svg.getBoundingClientRect(),W=Math.max(400,box.width),H=Math.max(300,box.height),top=95,bottom=72,left=18,right=76,ns='http://www.w3.org/2000/svg';svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const bs=bars.slice(-120), rawMin=Math.min(...bs.map(b=>b.low)),rawMax=Math.max(...bs.map(b=>b.high)),pad=Math.max((rawMax-rawMin)*.08,rawMax*.0003),lo=rawMin-pad,hi=rawMax+pad,range=hi-lo||1,plotBottom=H-bottom-42,maxVol=Math.max(...bs.map(b=>b.volume),1);
  const step=(W-left-right)/Math.max(1,bs.length),x=i=>left+(i+.5)*step,y=v=>top+(hi-v)/range*(plotBottom-top),bodyW=Math.max(2,Math.min(7,step*.66));
  const points=bs.map((b,i)=>[x(i),y(b.close)]),path=points.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ');$('chart-line').setAttribute('d',path);$('chart-area').setAttribute('d',path+` L${points.at(-1)[0]},${plotBottom} L${points[0][0]},${plotBottom} Z`);
  const grid=$('chart-grid'),axis=$('chart-axis');grid.innerHTML='';axis.innerHTML='';[.2,.5,.8].forEach(f=>{const yy=top+f*(plotBottom-top),line=document.createElementNS(ns,'line');line.setAttribute('x1',left);line.setAttribute('x2',W-right);line.setAttribute('y1',yy);line.setAttribute('y2',yy);line.setAttribute('class','grid');grid.append(line);const t=document.createElementNS(ns,'text');t.setAttribute('x',W-10);t.setAttribute('y',yy-5);t.setAttribute('text-anchor','end');t.setAttribute('class','axis');t.textContent=(hi-f*range).toFixed(2);axis.append(t)});
  const candles=$('chart-candles'),volumes=$('chart-volume');candles.innerHTML='';volumes.innerHTML='';bs.forEach((b,i)=>{const up=b.close>=b.open,klass=up?'candleUp':'candleDown',xx=x(i),wick=document.createElementNS(ns,'line');wick.setAttribute('x1',xx);wick.setAttribute('x2',xx);wick.setAttribute('y1',y(b.high));wick.setAttribute('y2',y(b.low));wick.setAttribute('class','wick '+klass);candles.append(wick);const rect=document.createElementNS(ns,'rect'),yt=Math.min(y(b.open),y(b.close)),bh=Math.max(1.5,Math.abs(y(b.open)-y(b.close)));rect.setAttribute('x',xx-bodyW/2);rect.setAttribute('y',yt);rect.setAttribute('width',bodyW);rect.setAttribute('height',bh);rect.setAttribute('rx','1');rect.setAttribute('class',klass);if(!b.complete)rect.setAttribute('opacity','.55');candles.append(rect);const vh=35*b.volume/maxVol,v=document.createElementNS(ns,'rect');v.setAttribute('x',xx-bodyW/2);v.setAttribute('y',H-bottom-vh);v.setAttribute('width',bodyW);v.setAttribute('height',vh);v.setAttribute('class',up?'volumeUp':'volumeDown');volumes.append(v)});
  const marks=$('chart-marks');marks.innerHTML='';const t0=new Date(bs[0].at).getTime(),t1=new Date(bs.at(-1).at).getTime();reviews.slice(-100).forEach(r=>{const rt=new Date(r.evaluated_at).getTime();if(rt<t0||rt>t1+60000)return;let idx=Math.round((rt-t0)/60000);idx=Math.max(0,Math.min(bs.length-1,idx));const c=document.createElementNS(ns,'circle'),d=r.direction?.choice;c.setAttribute('cx',x(idx));c.setAttribute('cy',y(bs[idx].close));c.setAttribute('r','4');c.setAttribute('fill',d==='up'?'var(--buy)':d==='down'?'var(--sell)':'#d5a342');c.setAttribute('class','marker');marks.append(c)});
  const first=bs[0].open,lastBar=bs.at(-1),live=latestQuote?.price??lastBar.close,qt=latestQuote?.quoted_at?new Date(latestQuote.quoted_at):new Date(lastBar.at),age=(Date.now()-qt.getTime())/1000,stale=age>60,change=live-first;text('s-price',money(live));text('hero-price',money(live));text('hero-change',(change>=0?'+':'')+change.toFixed(2)+' · '+(stale?'MARKET DATA STALE':'LIVE MOOMOO'));text('hero-time',qt.toLocaleString());$('dot').className=stale?'dot off':'dot';text('connection',stale?'market closed / stale':'live');
}
function renderChecks(id,checks){const el=$(id);el.innerHTML='';Object.entries(checks||{}).forEach(([name,ok])=>{const li=document.createElement('li'),a=document.createElement('span'),b=document.createElement('span');a.textContent=name;b.textContent=ok?'✓':'×';b.className=ok?'yes':'no';li.append(a,b);el.append(li)})}
function renderSetup(data){if(!data)return;const s=data.summary;text('setup-call',data.action.headline);text('setup-detail',data.action.detail);renderChecks('buy-checks',s.buy_checks);renderChecks('sell-checks',s.sell_checks)}
function renderExecution(x){
  if(!x){text('execution','Auto trading is not running');$('kill').style.display='none';return}
  text('mode',x.jev_gate_mode||'shadow');text('s-trades',x.trades_today);
  const held=x.position?`${x.position.quantity} shares @ ${x.position.entry_price.toFixed(2)} · P&L ${x.daily_pnl.toFixed(2)}`:null;
  let msg=x.halted?'STOPPED · '+x.halted+(held?' · still holding '+held:''):x.pending_exit?`Selling ${x.pending_exit.quantity} shares (${x.pending_exit.reason})`:held?held:x.pending_order?`Limit buy ${x.pending_order.quantity} @ ${x.pending_order.limit_price.toFixed(2)}`:'Watching for entries';
  if(x.broker_error)msg+=' · broker error: '+x.broker_error;
  text('execution',x.environment+' · '+msg+(x.last_skip?' · '+x.last_skip:''));$('kill').style.display='inline-block'}
async function refreshSetup(){try{const r=await fetch('/api/data',{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.error||'setup unavailable');errors.setup=null;setupData=d;renderSetup(d)}catch(e){errors.setup=e.message}showErrors()}
async function refreshLive(){try{const r=await fetch('/api/live',{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.error||'live data unavailable');errors.live=null;errors.quote=d.latest_quote_error;liveData=d;renderReview(d.latest_review);renderFeed(d.reviews);renderStrip(d.reviews);renderChart(d.bars,d.reviews,d.latest_quote);text('s-calls',d.review_count);if(d.latest_quote_error){$('dot').className='dot off';text('connection','feed error')}renderExecution(d.execution)}catch(e){$('dot').className='dot off';text('connection','reconnecting');errors.live=e.message}showErrors()}
$('kill').addEventListener('click',async()=>{if(confirm('Cancel entries, flatten the position, and stop trading?'))await fetch('/api/flatten',{method:'POST',headers:{'{{ flatten_header }}':'{{ flatten_header_value }}'}})});
refreshSetup();refreshLive();setInterval(refreshSetup,5000);setInterval(refreshLive,2000);window.addEventListener('resize',()=>liveData&&renderChart(liveData.bars,liveData.reviews,liveData.latest_quote));
</script>
</body></html>
"""


def action_for(setup: Setup) -> dict:
    """Plain-language instruction for the current deterministic setup."""
    if setup.signal in BUY_SIGNALS or setup.signal in SELL_SIGNALS:
        side = "BUY" if setup.signal in BUY_SIGNALS else "SELL"
        if setup.stop is None or setup.target is None:
            return {"headline": f"{side} signal without a valid stop", "detail": f"{setup.signal}: skip; stop equals price"}
        risk = abs(setup.price - setup.stop)
        return {
            "headline": f"{side} near {setup.price:.2f}",
            "detail": f"{setup.signal} · stop {setup.stop:.2f} · target {setup.target:.2f} · risk {risk:.2f}/share · exit by 15:50",
        }
    if setup.signal.startswith("HOLD ("):
        return {"headline": "No trade", "detail": setup.signal[len("HOLD ("):-1].capitalize()}
    missing = [name for name, ok in setup.buy_checks.items() if not ok]
    return {"headline": "No trade", "detail": "Waiting for: " + ", ".join(missing)}


def snapshot_payload(snapshot: Snapshot, ai: dict) -> dict:
    setup = snapshot.setup
    return {
        "summary": {
            "timestamp": setup.timestamp.isoformat(), "published_at": snapshot.published_at.isoformat(),
            "price": setup.price, "signal": setup.signal, "buy_score": setup.buy_score,
            "sell_score": setup.sell_score, "total_checks": setup.total_checks,
            "buy_checks": setup.buy_checks, "sell_checks": setup.sell_checks, "rsi": setup.rsi,
            "relative_volume": setup.relative_volume, "ema_fast": setup.ema_fast,
            "ema_slow": setup.ema_slow, "vwap": setup.vwap, "stop": setup.stop, "target": setup.target,
        },
        "action": action_for(setup), "ai": ai,
    }


def quote_payload(quote: LiveQuote) -> dict:
    forming = quote.forming
    return {
        "symbol": quote.symbol, "price": quote.price,
        "quoted_at": quote.quoted_at.isoformat() if quote.quoted_at is not None else None,
        "published_at": quote.published_at.isoformat(),
        "forming": None if forming is None else {
            "time": forming.start.isoformat(), "open": forming.open, "high": forming.high,
            "low": forming.low, "close": forming.close, "volume": forming.volume,
        },
    }


def execution_payload(state: ExecutionState, environment: str, jev_gate_mode: str = "shadow") -> dict:
    position, pending, exiting = state.position, state.pending_order, state.pending_exit
    return {
        "environment": environment, "jev_gate_mode": jev_gate_mode, "halted": state.halted,
        "broker_error": state.broker_error,
        "last_skip": state.last_skip, "trades_today": state.trades_today, "daily_pnl": state.daily_pnl,
        "position": None if position is None else {
            "quantity": position.quantity, "entry_price": position.entry_price, "stop": position.stop,
            "target": position.target, "opened_at": position.opened_at.isoformat(),
        },
        "pending_order": None if pending is None else {
            "order_id": pending.order_id, "quantity": pending.quantity,
            "limit_price": pending.limit_price, "placed_at": pending.placed_at.isoformat(),
            "cancel_requested": pending.cancel_requested,
        },
        "pending_exit": None if exiting is None else {
            "order_id": exiting.order_id, "quantity": exiting.quantity,
            "reason": exiting.reason, "placed_at": exiting.placed_at.isoformat(),
        },
        "fills": [{"side": f.side, "quantity": f.quantity, "price": f.price, "at": f.at.isoformat(), "reason": f.reason} for f in state.fills],
    }


def create_app(store: SignalStore, review: Callable[[Snapshot], dict], environment: str = "SIMULATE", jev_gate_mode: str = "shadow") -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = False
    app.config["TRUSTED_HOSTS"] = TRUSTED_HOSTS

    @app.get("/")
    def index():
        snapshot: Optional[Snapshot] = store.latest()
        return render_template_string(
            DASHBOARD,
            symbol=snapshot.symbol if snapshot else "Watcher",
            benchmark=snapshot.benchmark if snapshot else "benchmark",
            flatten_header=FLATTEN_HEADER,
            flatten_header_value=FLATTEN_HEADER_VALUE,
        )

    @app.get("/api/data")
    def data():
        snapshot = store.latest()
        if snapshot is None:
            return jsonify(error="No completed candle has been evaluated yet; the watcher publishes once per minute"), 503
        if snapshot.error is not None:
            return jsonify(error=snapshot.error), 503
        return jsonify(snapshot_payload(snapshot, review(snapshot)))

    @app.get("/api/quote")
    def quote():
        latest = store.latest_quote()
        if latest is None:
            return jsonify(error="No live quote has arrived yet"), 503
        if latest.error is not None:
            return jsonify(error=latest.error), 503
        return jsonify(quote_payload(latest))

    @app.get("/api/live")
    def live():
        snapshot = store.latest()
        bars = []
        if snapshot is not None and snapshot.session is not None:
            for at, row in snapshot.session.tail(390).iterrows():
                bars.append({
                    "at": at.isoformat(), "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]), "volume": int(row["volume"]),
                    "complete": True,
                })
        latest_quote = store.latest_quote()
        if latest_quote is not None and latest_quote.forming is not None:
            candle = latest_quote.forming
            forming = {
                "at": candle.start.isoformat(), "open": candle.open, "high": candle.high,
                "low": candle.low, "close": candle.close, "volume": candle.volume, "complete": False,
            }
            if bars and bars[-1]["at"] == forming["at"]:
                bars[-1] = forming
            else:
                bars.append(forming)
        state = store.latest_execution()
        execution = execution_payload(state, environment, jev_gate_mode) if state is not None else None
        return jsonify(
            bars=bars,
            latest_quote=quote_payload(latest_quote) if latest_quote is not None and latest_quote.error is None else None,
            latest_quote_error=latest_quote.error if latest_quote is not None else None,
            reviews=store.jev_history(),
            review_count=store.jev_completed(),
            latest_review=store.latest_jev(),
            execution=execution,
        )

    @app.get("/api/execution")
    def execution():
        state = store.latest_execution()
        if state is None:
            return jsonify(error="Auto trading is not running"), 503
        return jsonify(execution_payload(state, environment, jev_gate_mode))

    @app.post("/api/flatten")
    def flatten():
        if request.headers.get(FLATTEN_HEADER) != FLATTEN_HEADER_VALUE:
            return jsonify(
                error=f"the kill switch requires the header {FLATTEN_HEADER}: {FLATTEN_HEADER_VALUE}; "
                "a page on another origin cannot send it, which is what keeps cross-site pages from flattening the account"
            ), 403
        store.pull_kill_switch()
        return jsonify(status="kill switch pulled; the executor flattens on its next pass")

    return app

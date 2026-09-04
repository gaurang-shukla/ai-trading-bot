const app = document.getElementById('app');
const cache = new Map();
const markets = [
  {id:'crypto_futures',name:'Crypto futures',icon:'₿',desc:'WEEX perpetual markets, funding and momentum'},
  {id:'crypto_spot',name:'Crypto spot',icon:'◆',desc:'Spot coins and 24-hour market breadth'},
  {id:'equities',name:'Stocks',icon:'▥',desc:'US and Indian companies through OpenBB'},
  {id:'forex',name:'Forex',icon:'↔',desc:'Major global currency pairs'},
  {id:'commodities',name:'Commodities',icon:'◈',desc:'Metals, energy and agricultural markets'},
  {id:'options',name:'Options',icon:'⌁',desc:'Options research by underlying company'}
];
const names = Object.fromEntries(markets.map(m => [m.id,m.name]));
const venueFor = market => market.startsWith('crypto_') ? 'weex' : 'openbb';
const safe = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const number = value => value == null ? '—' : Number(value).toLocaleString(undefined,{maximumFractionDigits:8});
const money = value => value == null ? '—' : Intl.NumberFormat(undefined,{notation:'compact',style:'currency',currency:'USD',maximumFractionDigits:2}).format(value);
const pct = value => value == null ? '—' : `${value > 0 ? '+' : ''}${Number(value).toFixed(2)}%`;
const scoreClass = value => value >= 70 ? 'high' : value >= 50 ? 'mid' : 'low';

function navigate(path){history.pushState({},'',path);render();}
document.addEventListener('click',event=>{const link=event.target.closest('[data-link]');if(!link)return;event.preventDefault();navigate(link.getAttribute('href'));});
window.addEventListener('popstate',render);

async function getJSON(url, options){const response=await fetch(url,options);const data=await response.json();if(!response.ok)throw new Error(data.detail||'Unable to load data');return data;}
async function getOverview(market){if(!cache.has(market))cache.set(market,getJSON(`/api/overview/${market}`));return cache.get(market);}

function pageHeader(eyebrow,title,copy,action=''){return `<div class="page-head"><div><p class="eyebrow">${eyebrow}</p><h1>${title}</h1><p>${copy}</p></div>${action}</div>`;}
function errorView(error){app.innerHTML=`<div class="error-state"><h2>We couldn’t load this market</h2><p>${safe(error.message)}</p><a class="primary" href="/" data-link>Choose another market</a></div>`;}

async function home(){
  app.innerHTML=pageHeader('START WITH ONE CHOICE','Which market interests you?','Pick a market first. Signal will then show its full universe, market mood, movers and AI-ranked setups.')+`<section class="market-grid">${markets.map(m=>`<a class="market-card" href="/market/${m.id}" data-link><span class="market-icon">${m.icon}</span><span class="arrow">→</span><h2>${m.name}</h2><p>${m.desc}</p></a>`).join('')}</section><section id="layers" class="layer-strip"><div class="layer"><small>Checking intelligence layers…</small></div></section>`;
  try{const data=await getJSON('/api/status');document.getElementById('layers').innerHTML=Object.entries(data.integrations).map(([key,item])=>{const ready=item.ready;return `<div class="layer"><b>${key==='tradingagents'?'TradingAgents':key[0].toUpperCase()+key.slice(1)}</b><small class="${ready?'ready':'needs'}">${ready?'Configured':'Setup Needed'}</small></div>`}).join('')}catch{document.getElementById('layers').innerHTML='<div class="layer"><small class="needs">Backend unavailable</small></div>'}
}

function moverRows(rows){return rows.length?rows.slice(0,4).map(row=>`<div class="mover"><b>${safe(row.symbol)}</b><span>${number(row.price)}</span><span class="${row.change>=0?'positive':'negative'}">${pct(row.change)}</span></div>`).join(''):'<p class="source-note">Mover data appears when this market feed is connected.</p>';}
function assetRows(rows){return rows.map(row=>`<tr><td><a href="/asset/${safe(location.pathname.split('/').pop())}/${safe(row.symbol)}" data-link>${safe(row.symbol)}</a></td><td>${number(row.price)}</td><td class="${row.change>=0?'positive':'negative'}">${pct(row.change)}</td><td>${money(row.volume)}</td><td>${row.signal_score==null?'—':`<span class="score ${scoreClass(row.signal_score)}">${row.signal_score}</span>`}</td><td><a href="/asset/${safe(location.pathname.split('/').pop())}/${safe(row.symbol)}" data-link>Analyse →</a></td></tr>`).join('');}

async function marketPage(market){
  app.innerHTML='<div class="loading-page"><div class="spinner"></div><p>Loading the market universe…</p></div>';
  try{const data=await getOverview(market);const mood=data.fear_greed;app.innerHTML=`<a class="back" href="/" data-link>← All markets</a>${pageHeader('MARKET OVERVIEW',names[market]||market,'See the whole market before considering any one asset.',`<a class="primary" href="/scores/${market}" data-link>View AI scores →</a>`)}<section class="summary-grid"><article class="panel"><h2>Signal Fear & Greed</h2>${mood?`<div class="gauge"></div><div class="gauge-score">${mood.score}</div><div class="gauge-label">${mood.label}</div><p class="source-note">${mood.method}; not an external index.</p>`:'<div class="empty-state"><p>Calculated after enough live OpenBB breadth data is available.</p></div>'}</article><article class="panel"><h2>Top gainers</h2><div class="mover-list">${moverRows(data.gainers)}</div><h2 style="margin-top:22px">Top losers</h2><div class="mover-list">${moverRows(data.losers)}</div></article><article class="panel"><h2>Market breadth</h2><div class="breadth"><div class="metric"><span>Tracked assets</span><strong>${number(data.summary.assets)}</strong></div><div class="metric"><span>Advancing</span><strong>${data.summary.advancers_pct==null?'—':data.summary.advancers_pct+'%'}</strong></div><div class="metric"><span>24h turnover</span><strong>${money(data.summary.total_quote_volume)}</strong></div></div><div class="spark"></div><p class="source-note">Source: ${safe(data.source)}</p></article></section><div class="toolbar"><div class="tabs"><a class="active">All assets</a><a href="/scores/${market}" data-link>AI rankings</a></div><input id="search" class="search" placeholder="Search symbol" aria-label="Search symbol"></div><div class="table-wrap"><table class="market-table"><thead><tr><th>Asset</th><th>Price</th><th>24h change</th><th>Volume</th><th>Screen score</th><th></th></tr></thead><tbody id="asset-body">${assetRows(data.assets)}</tbody></table></div>`;document.getElementById('search').addEventListener('input',e=>{const q=e.target.value.trim().toUpperCase();document.getElementById('asset-body').innerHTML=assetRows(data.assets.filter(row=>row.symbol.includes(q)))})}catch(error){errorView(error)}
}

async function scoresPage(market){
  app.innerHTML='<div class="loading-page"><div class="spinner"></div><p>Building the ranked setup list…</p></div>';
  try {
    const data=await getOverview(market);
    const rows=[...data.assets].filter(r=>r.signal_score!=null).sort((a,b)=>b.signal_score-a.signal_score);
    const table=rows.length
      ? `<div class="table-wrap"><table class="market-table"><thead><tr><th>Rank</th><th>Asset</th><th>Signal score</th><th>Trend</th><th>Sentiment</th><th>Liquidity</th><th>Risk quality</th><th></th></tr></thead><tbody id="score-body">${scoreRows(rows,market)}</tbody></table></div>`
      : `<div class="empty-state"><h2>Connect the live ${safe(names[market])} feed</h2><p>Rankings are withheld until genuine quotes and breadth data are available. You can still analyse any listed asset from the market page.</p></div>`;
    app.innerHTML=`<a class="back" href="/market/${market}" data-link>← ${safe(names[market])} overview</a><section class="score-hero"><div class="panel">${pageHeader('AI SETUP SCREENER','Ranked opportunities','The fast screen narrows the market using price trend, market mood, liquidity and volatility. Open any asset for the slower TradingAgents debate.')}</div><div class="panel"><h2>How scoring works</h2><div class="formula"><div><b>42%</b><small>Trend</small></div><div><b>18%</b><small>Mood</small></div><div><b>22%</b><small>Liquidity</small></div><div><b>18%</b><small>Risk</small></div></div></div></section><div class="toolbar"><div class="tabs"><a href="/market/${market}" data-link>Market</a><a class="active">AI rankings</a></div><input id="search" class="search" placeholder="Search ranked assets"></div>${table}`;
    if(rows.length)document.getElementById('search').addEventListener('input',e=>{const q=e.target.value.trim().toUpperCase();document.getElementById('score-body').innerHTML=scoreRows(rows.filter(r=>r.symbol.includes(q)),market)});
  } catch(error) { errorView(error); }
}
function scoreRows(rows,market){return rows.map((r,i)=>`<tr><td>${i+1}</td><td><a href="/asset/${market}/${safe(r.symbol)}" data-link>${safe(r.symbol)}</a></td><td><span class="score ${scoreClass(r.signal_score)}">${r.signal_score}</span></td><td>${r.trend_score}</td><td>${r.sentiment_score}</td><td>${r.liquidity_score}</td><td>${r.risk_score}</td><td><a href="/asset/${market}/${safe(r.symbol)}" data-link>Deep analysis →</a></td></tr>`).join('');}

async function assetPage(market,symbol){
  app.innerHTML=`<a class="back" href="/scores/${market}" data-link>← AI rankings</a>${pageHeader('THREE-LAYER DEEP ANALYSIS',safe(symbol),'TradingAgents is debating technical, fundamental, sentiment and risk evidence. This can take several minutes.')}<div class="loading-page"><div class="spinner"></div><p>Research agents are working…</p></div>`;
  try{const data=await getJSON('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({market,venue:venueFor(market),symbol,equity:100000})});const side=data.signal.side;const aiNotice=data.ai_available===false?`<p class="warning">${safe(data.ai_notice)}</p>`:'';app.innerHTML=`<a class="back" href="/scores/${market}" data-link>← AI rankings</a>${pageHeader('THREE-LAYER DEEP ANALYSIS',safe(symbol),`Live reference price: ${number(data.market.price)} · ${safe(data.market.source)}`)}${aiNotice}<section class="detail-grid"><article class="panel"><p class="eyebrow">TRADINGAGENTS DECISION</p><div class="decision ${side==='BUY'?'positive':side==='SELL'?'negative':''}">${safe(side)}</div><p>Confidence ${Math.round(data.signal.confidence*100)}%</p><div class="confidence-bar"><span style="width:${Math.round(data.signal.confidence*100)}%"></span></div><h2>Safety gate</h2><p class="${data.risk.approved?'positive':'negative'}">${data.risk.approved?'Approved for paper simulation':'Blocked'}</p><p>${safe(data.risk.reason)}</p></article><article class="panel"><p class="eyebrow">PLAIN-LANGUAGE RESEARCH</p><div class="plain-copy">${safe(data.signal.rationale)}</div><p class="warning">No live WEEX order was placed. Paperclip records this decision only when its bridge is configured.</p></article></section>`}catch(error){errorView(error)}
}

function render(){const parts=location.pathname.split('/').filter(Boolean);if(!parts.length)return home();if(parts[0]==='market'&&parts[1])return marketPage(parts[1]);if(parts[0]==='scores'&&parts[1])return scoresPage(parts[1]);if(parts[0]==='asset'&&parts[1]&&parts[2])return assetPage(parts[1],decodeURIComponent(parts.slice(2).join('/')));return home();}

let installPrompt;window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installPrompt=e});document.getElementById('install').addEventListener('click',async()=>{if(installPrompt){installPrompt.prompt();await installPrompt.userChoice;installPrompt=null}else alert('In Chrome, open the menu and choose “Cast, save and share” → “Install page as app”.')});if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js');render();

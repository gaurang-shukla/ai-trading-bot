const app = document.getElementById('app');
const cache = new Map();
const deepJobKeys = new Map();
const deepProgressSteps=['Preparing market data','Checking technical indicators','Running TradingAgents research','Building plain-language summary','Finalizing decision'];
const markets = [
  {id:'crypto_futures',name:'Crypto futures',icon:'₿',desc:'WEEX perpetual markets, funding and momentum'},
  {id:'crypto_spot',name:'Crypto spot',icon:'◆',desc:'Spot coins and 24-hour market breadth'},
  {id:'equities',name:'Stocks',icon:'▥',desc:'US and Indian companies through OpenBB'},
  {id:'forex',name:'Forex',icon:'↔',desc:'Major global currency pairs'},
  {id:'commodities',name:'Commodities',icon:'◈',desc:'Metals, energy and agricultural markets'},
  {id:'indian_indices',name:'Indian Indices',icon:'₹',desc:'NIFTY, BANK NIFTY and Indian market indices'},
  {id:'banknifty_options',name:'Bank Nifty Options',icon:'₹',desc:'CE/PE strikes, expiry, OI and option signals'}
];
const displayNames = {BANKNIFTY:'BANK NIFTY',NIFTY50:'NIFTY 50',FINNIFTY:'FINNIFTY',MIDCPNIFTY:'MIDCPNIFTY'};
const displaySymbol = symbol => displayNames[symbol] || symbol;
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
  try{const data=await getJSON('/api/status');document.getElementById('layers').innerHTML=Object.entries(data.integrations).filter(([key,item])=>key!=='paperclip'||item.enabled).map(([key,item])=>{const ready=item.ready;return `<div class="layer"><b>${key==='tradingagents'?'TradingAgents':key[0].toUpperCase()+key.slice(1)}</b><small class="${ready?'ready':'needs'}">${ready?'Connected':'Setup Needed'}</small></div>`}).join('')}catch{document.getElementById('layers').innerHTML='<div class="layer"><small class="needs">Backend unavailable</small></div>'}
}

function moverRows(rows){return rows.length?rows.slice(0,4).map(row=>`<div class="mover"><b>${safe(displaySymbol(row.symbol))}</b><span>${number(row.price)}</span><span class="${row.change>=0?'positive':'negative'}">${pct(row.change)}</span></div>`).join(''):'<p class="source-note">Mover data appears when this market feed is connected.</p>';}
function assetRows(rows){return rows.map(row=>`<tr><td><a href="/asset/${safe(location.pathname.split('/').pop())}/${safe(row.symbol)}" data-link>${safe(row.display_name||displaySymbol(row.symbol))}</a></td><td>${number(row.price)}</td><td class="${row.change>=0?'positive':'negative'}">${pct(row.change)}</td><td>${money(row.volume)}</td><td>${row.signal_score==null?'—':`<span class="score ${scoreClass(row.signal_score)}">${row.signal_score}</span>`}</td><td><a href="/asset/${safe(location.pathname.split('/').pop())}/${safe(row.symbol)}" data-link>Analyse →</a></td></tr>`).join('');}

async function marketPage(market){
  if(market==='options'){app.innerHTML=`<a class="back" href="/" data-link>← All markets</a>${pageHeader('NOT CONNECTED YET','Options','A real generic options provider is not connected yet.')}<div class="empty-state panel"><p>No blank assets or generated option rows are shown.</p></div>`;return}
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
function scoreRows(rows,market){return rows.map((r,i)=>`<tr><td>${i+1}</td><td><a href="/asset/${market}/${safe(r.symbol)}" data-link>${safe(displaySymbol(r.symbol))}</a></td><td><span class="score ${scoreClass(r.signal_score)}">${r.signal_score}</span></td><td>${r.trend_score}</td><td>${r.sentiment_score}</td><td>${r.liquidity_score}</td><td>${r.risk_score}</td><td><a href="/asset/${market}/${safe(r.symbol)}" data-link>Deep analysis →</a></td></tr>`).join('');}

const chartFrames=['1m','5m','15m','1h','4h','1d'];
function placeChartLabels(labels,top,bottom,spacing=18){
  const placed=labels.map(label=>({...label,labelY:Math.max(top,Math.min(bottom,label.lineY))})).sort((a,b)=>a.labelY-b.labelY);
  for(let i=1;i<placed.length;i++)placed[i].labelY=Math.max(placed[i].labelY,placed[i-1].labelY+spacing);
  if(placed.length&&placed.at(-1).labelY>bottom){
    placed.at(-1).labelY=bottom;
    for(let i=placed.length-2;i>=0;i--)placed[i].labelY=Math.min(placed[i].labelY,placed[i+1].labelY-spacing);
  }
  if(placed.length&&placed[0].labelY<top){
    placed[0].labelY=top;
    for(let i=1;i<placed.length;i++)placed[i].labelY=Math.max(placed[i].labelY,placed[i-1].labelY+spacing);
  }
  return placed;
}
function svgPriceChart(data,selectedFrame){
  const series=data.chart_timeframes||{};
  const fallback=(data.chart_points||[]).map(p=>({timestamp:p.timestamp,open:p.close,high:p.close,low:p.close,close:p.close}));
  const available=chartFrames.filter(frame=>(series[frame]||[]).length>1);
  const frame=(selectedFrame&&available.includes(selectedFrame)?selectedFrame:null)||data.chart_default_timeframe||available[0];
  const bars=(series[frame]||fallback).map(bar=>Object.fromEntries(Object.entries(bar).map(([key,value])=>[key,key==='timestamp'?value:Number(value)]))).filter(bar=>['open','high','low','close'].every(key=>Number.isFinite(bar[key])));
  const buttons=`<div class="chart-toolbar"><div><p class="eyebrow">MARKET CHART</p><strong>${safe(frame||'Price history')}</strong></div><div class="timeframe-buttons" role="group" aria-label="Chart timeframe">${chartFrames.map(item=>`<button type="button" data-chart-frame="${item}" class="${item===frame?'active':''}" ${series[item]?.length>1?'':'disabled'}>${item}</button>`).join('')}</div></div>`;
  if(bars.length<2)return `<div class="price-chart">${buttons}<div class="chart-unavailable"><span>Chart unavailable</span><small>Candle history is not available for this asset.</small></div></div>`;
  const width=720,height=350,left=14,right=112,top=18,bottom=66,plotRight=width-right,priceBottom=height-bottom;
  const rawLevels={support:data.key_levels?.support,resistance:data.key_levels?.resistance,stop_loss:data.key_levels?.stop_loss,take_profit:data.key_levels?.take_profit,current_price:data.live_price??data.market?.price};
  const values=[...bars.flatMap(b=>[b.high,b.low]),...Object.values(rawLevels).map(Number).filter(Number.isFinite)];
  let min=Math.min(...values),max=Math.max(...values),range=max-min||Math.abs(max)||1;min-=range*.06;max+=range*.06;range=max-min;
  const y=value=>top+(max-value)*(priceBottom-top)/range;
  const step=(plotRight-left)/bars.length,bodyWidth=Math.max(2,Math.min(8,step*.62));
  const grid=Array.from({length:5},(_,i)=>{const gy=top+i*(priceBottom-top)/4,value=max-i*range/4;return `<line x1="${left}" x2="${plotRight}" y1="${gy}" y2="${gy}" class="chart-grid"/><text x="${plotRight+8}" y="${gy+4}" class="chart-axis">${safe(number(value))}</text>`}).join('');
  const candles=bars.map((bar,i)=>{const x=left+step*(i+.5),openY=y(bar.open),closeY=y(bar.close),up=bar.close>=bar.open;return `<g class="candle ${up?'up':'down'}" data-candle-index="${i}" data-x="${x}" data-time="${safe(bar.timestamp)}" data-open="${bar.open}" data-high="${bar.high}" data-low="${bar.low}" data-close="${bar.close}" data-volume="${Number.isFinite(bar.volume)?bar.volume:''}"><line x1="${x}" x2="${x}" y1="${y(bar.high)}" y2="${y(bar.low)}"/><rect x="${x-bodyWidth/2}" y="${Math.min(openY,closeY)}" width="${bodyWidth}" height="${Math.max(1.5,Math.abs(closeY-openY))}" rx=".7"/></g>`}).join('');
  const maxVolume=Math.max(...bars.map(bar=>Number(bar.volume)||0),1),volumeTop=priceBottom+12;
  const volumes=bars.map((bar,i)=>{const value=Number(bar.volume)||0,x=left+step*(i+.5),h=(height-8-volumeTop)*value/maxVolume;return `<rect class="volume-bar ${bar.close>=bar.open?'up':'down'}" x="${x-bodyWidth/2}" y="${height-8-h}" width="${bodyWidth}" height="${h}"/>`}).join('');
  const labels=placeChartLabels(Object.entries(rawLevels).map(([key,value],index)=>({key,value:Number(value),lineY:y(Number(value)),index})).filter(item=>Number.isFinite(item.value)),top+9,priceBottom-9);
  const overlays=labels.map(item=>{const title=item.key.replace('_',' '),pillWidth=Math.max(70,title.length*6.2+12);return `<g class="level-overlay level-${item.index}"><line x1="${left}" x2="${plotRight}" y1="${item.lineY}" y2="${item.lineY}" class="chart-level"/><path d="M ${plotRight} ${item.lineY} L ${plotRight+7} ${item.labelY}" class="label-leader"/><rect x="${plotRight-pillWidth-5}" y="${item.labelY-8}" width="${pillWidth}" height="16" rx="8" class="chart-label-pill"/><text x="${plotRight-11}" y="${item.labelY+3.5}" text-anchor="end" class="chart-label">${safe(title)} · ${safe(number(item.value))}</text></g>`}).join('');
  const quote=location.pathname.split('/')[2]?.startsWith('crypto_')?'USDT':'';
  return `<div class="price-chart" data-active-frame="${safe(frame)}" data-hover-enabled="true">${buttons}<div class="chart-stage"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${safe(frame)} candlestick price chart"><g class="chart-grid-layer">${grid}</g><g class="chart-volume" aria-label="Volume">${volumes}</g>${candles}<g class="chart-crosshair" aria-hidden="true"><line class="crosshair-x" x1="0" x2="0" y1="${top}" y2="${priceBottom}"/><line class="crosshair-y" x1="${left}" x2="${plotRight}" y1="0" y2="0"/></g>${overlays}</svg><div class="chart-tooltip" role="status" aria-live="polite" data-quote="${quote}" hidden><strong data-field="time">—</strong><div><span>Open</span><b data-field="open">—</b></div><div><span>High</span><b data-field="high">—</b></div><div><span>Low</span><b data-field="low">—</b></div><div><span>Close</span><b data-field="close">—</b></div><div><span>Volume</span><b data-field="volume">—</b></div></div></div><div class="chart-caption"><span>${bars.length} candles · hover or tap for OHLCV</span><span>High ${number(max)} · Low ${number(min)}</span></div></div>`;
}
const LIGHTWEIGHT_CHARTS_URL='https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js';
let lightweightChartsPromise;
function loadLightweightCharts(){
  if(window.LightweightCharts)return Promise.resolve(window.LightweightCharts);
  if(!lightweightChartsPromise)lightweightChartsPromise=new Promise((resolve,reject)=>{
    const script=document.createElement('script');script.src=LIGHTWEIGHT_CHARTS_URL;script.async=true;script.crossOrigin='anonymous';
    script.onload=()=>window.LightweightCharts?resolve(window.LightweightCharts):reject(new Error('Lightweight Charts did not initialize'));
    script.onerror=()=>reject(new Error('Lightweight Charts failed to load'));document.head.appendChild(script);
  });
  return lightweightChartsPromise;
}
function chartTime(value){
  if(typeof value==='number'||/^\d+(\.\d+)?$/.test(String(value))){const numeric=Number(value);return Math.floor(numeric>1e12?numeric/1000:numeric)}
  const parsed=Date.parse(value);return Number.isFinite(parsed)?Math.floor(parsed/1000):null;
}
function mapCandlesToLightweight(bars){
  const mapped=(bars||[]).map(bar=>({time:chartTime(bar.timestamp??bar.time),open:Number(bar.open),high:Number(bar.high),low:Number(bar.low),close:Number(bar.close)})).filter(bar=>bar.time!=null&&['open','high','low','close'].every(key=>Number.isFinite(bar[key]))).sort((a,b)=>a.time-b.time);
  return mapped.filter((bar,index)=>!index||bar.time!==mapped[index-1].time);
}
function mapVolumeToLightweight(bars){
  return (bars||[]).map(bar=>({time:chartTime(bar.timestamp??bar.time),value:Number(bar.volume),color:Number(bar.close)>=Number(bar.open)?'rgba(36, 211, 149, .38)':'rgba(255, 91, 111, .38)'})).filter(bar=>bar.time!=null&&Number.isFinite(bar.value)&&bar.value>=0).sort((a,b)=>a.time-b.time);
}
function selectedChartData(data,selectedFrame){
  const series=data.chart_timeframes||{},fallback=(data.chart_points||[]).map(p=>({timestamp:p.timestamp,open:p.close,high:p.close,low:p.close,close:p.close}));
  const available=chartFrames.filter(frame=>(series[frame]||[]).length>1),frame=(selectedFrame&&available.includes(selectedFrame)?selectedFrame:null)||data.chart_default_timeframe||available[0];
  return {series,available,frame,bars:series[frame]||fallback};
}
function chartToolbar(series,frame){return `<div class="chart-toolbar"><div><p class="eyebrow">MARKET CHART</p><strong>${safe(frame||'Price history')}</strong></div><div class="timeframe-buttons" role="group" aria-label="Chart timeframe">${chartFrames.map(item=>`<button type="button" data-chart-frame="${item}" class="${item===frame?'active':''}" ${series[item]?.length>1?'':'disabled'}>${item}</button>`).join('')}</div></div>`}
function priceChart(data,selectedFrame){
  const {series,frame,bars}=selectedChartData(data,selectedFrame),candles=mapCandlesToLightweight(bars);
  const buttons=chartToolbar(series,frame);
  if(candles.length<2)return `<div class="price-chart">${buttons}<div class="chart-unavailable"><span>Chart unavailable</span><small>Candle history is not available for this asset.</small></div></div>`;
  return `<div class="price-chart lightweight-price-chart" data-active-frame="${safe(frame)}">${buttons}<div class="advanced-chart-shell"><div class="lightweight-chart-container" role="img" aria-label="${safe(frame)} interactive candlestick chart"></div><div class="lightweight-ohlc" role="status" aria-live="polite"></div></div><div class="chart-fallback" hidden>${svgPriceChart(data,frame)}<p class="fallback-note">Advanced chart unavailable; showing basic chart.</p></div><div class="chart-level-legend" aria-label="Chart price levels"></div><a class="chart-attribution" href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">Charts by TradingView</a></div>`;
}
function priceLineConfiguration(data,bars){
  const levels=[['Support',data.key_levels?.support,'#24d395'],['Resistance',data.key_levels?.resistance,'#f7bd52'],['Stop',data.key_levels?.stop_loss,'#ff5b6f'],['Take profit',data.key_levels?.take_profit,'#559cff'],['Current',data.live_price??data.market?.price,'#d8e5ef']];
  const prices=mapCandlesToLightweight(bars).flatMap(bar=>[bar.high,bar.low]),low=Math.min(...prices),high=Math.max(...prices),range=high-low||Math.abs(high)||1;
  return levels.map(([title,value,color])=>({title,price:Number(value),color,lineWidth:title==='Current'?2:1,lineStyle:title==='Current'?0:2,axisLabelVisible:true,inScale:Number.isFinite(Number(value))&&Number(value)>=low-range*.35&&Number(value)<=high+range*.35})).filter(level=>Number.isFinite(level.price));
}
function showSvgFallback(container){
  container.querySelector('.advanced-chart-shell')?.setAttribute('hidden','');const fallback=container.querySelector('.chart-fallback');if(fallback){fallback.hidden=false;bindChartHover(fallback)}
}
async function mountLightweightChart(container,data,selectedFrame){
  const target=container.querySelector('.lightweight-chart-container');if(!target)return;
  try{
    const LC=await loadLightweightCharts();if(!target.isConnected)return;
    const {bars}=selectedChartData(data,selectedFrame),candles=mapCandlesToLightweight(bars),volume=mapVolumeToLightweight(bars);
    const chart=LC.createChart(target,{autoSize:false,layout:{background:{type:'solid',color:'#071722'},textColor:'#8fa7b9',fontFamily:'Inter,system-ui,sans-serif'},grid:{vertLines:{color:'#122a39'},horzLines:{color:'#122a39'}},crosshair:{mode:LC.CrosshairMode.Normal},rightPriceScale:{visible:true,borderColor:'#294252',scaleMargins:{top:.08,bottom:volume.length?.23:.08}},timeScale:{visible:true,borderColor:'#294252',timeVisible:true,secondsVisible:false,rightOffset:5,barSpacing:8},handleScroll:true,handleScale:true});
    const candleSeries=chart.addCandlestickSeries({upColor:'#24d395',downColor:'#ff5b6f',borderUpColor:'#24d395',borderDownColor:'#ff5b6f',wickUpColor:'#24d395',wickDownColor:'#ff5b6f',priceLineVisible:false,lastValueVisible:true});candleSeries.setData(candles);
    if(volume.length){const volumeSeries=chart.addHistogramSeries({priceFormat:{type:'volume'},priceScaleId:'volume',lastValueVisible:false,priceLineVisible:false});volumeSeries.priceScale().applyOptions({scaleMargins:{top:.78,bottom:0}});volumeSeries.setData(volume)}
    const levels=priceLineConfiguration(data,bars),legend=container.querySelector('.chart-level-legend');
    levels.filter(level=>level.inScale).forEach(level=>candleSeries.createPriceLine(level));legend.innerHTML=levels.map(level=>`<span style="--level-color:${level.color}"><i></i>${safe(level.title)} <b>${number(level.price)}</b>${level.inScale?'':' · off scale'}</span>`).join('');
    const ohlc=container.querySelector('.lightweight-ohlc'),renderOhlc=bar=>{ohlc.innerHTML=bar?`<span>O <b>${number(bar.open)}</b></span><span>H <b>${number(bar.high)}</b></span><span>L <b>${number(bar.low)}</b></span><span>C <b>${number(bar.close)}</b></span>`:''};renderOhlc(candles.at(-1));
    chart.subscribeCrosshairMove(param=>renderOhlc(param.seriesData.get(candleSeries)||candles.at(-1)));
    const resize=()=>chart.applyOptions({width:target.clientWidth,height:Math.max(300,Math.min(440,target.clientWidth*.54))});resize();const observer=new ResizeObserver(resize);observer.observe(target);chart.timeScale().fitContent();
    container._chartCleanup=()=>{observer.disconnect();chart.remove()};
  }catch(error){showSvgFallback(container)}
}
function bindChartHover(container){
  const stage=container.querySelector('.chart-stage'),svg=stage?.querySelector('svg'),tooltip=stage?.querySelector('.chart-tooltip'),candles=[...(svg?.querySelectorAll('[data-candle-index]')||[])];
  if(!stage||!svg||!tooltip||!candles.length)return;
  const xLine=svg.querySelector('.crosshair-x'),yLine=svg.querySelector('.crosshair-y'),field=name=>tooltip.querySelector(`[data-field="${name}"]`);
  const hide=()=>{tooltip.hidden=true;svg.querySelector('.chart-crosshair').classList.remove('visible')};
  const show=event=>{const bounds=svg.getBoundingClientRect(),pointX=Math.max(0,Math.min(bounds.width,event.clientX-bounds.left)),viewX=pointX*720/bounds.width,candle=candles.reduce((nearest,item)=>Math.abs(Number(item.dataset.x)-viewX)<Math.abs(Number(nearest.dataset.x)-viewX)?item:nearest),priceY=Math.max(18,Math.min(284,(event.clientY-bounds.top)*350/bounds.height));
    xLine.setAttribute('x1',candle.dataset.x);xLine.setAttribute('x2',candle.dataset.x);yLine.setAttribute('y1',priceY);yLine.setAttribute('y2',priceY);svg.querySelector('.chart-crosshair').classList.add('visible');
    const timestamp=Number(candle.dataset.time),date=Number.isFinite(timestamp)?new Date(timestamp<1e12?timestamp*1000:timestamp):new Date(candle.dataset.time);field('time').textContent=Number.isNaN(date.getTime())?candle.dataset.time:date.toLocaleString();['open','high','low','close'].forEach(name=>field(name).textContent=`${number(candle.dataset[name])}${tooltip.dataset.quote?' '+tooltip.dataset.quote:''}`);field('volume').textContent=number(candle.dataset.volume);
    tooltip.hidden=false;const tip=tooltip.getBoundingClientRect(),stageBox=stage.getBoundingClientRect(),gap=12;let left=event.clientX-stageBox.left+gap,top=event.clientY-stageBox.top+gap;if(left+tip.width>stageBox.width-gap)left=event.clientX-stageBox.left-tip.width-gap;if(top+tip.height>stageBox.height-gap)top=stageBox.height-tip.height-gap;tooltip.style.left=`${Math.max(gap,Math.min(left,stageBox.width-tip.width-gap))}px`;tooltip.style.top=`${Math.max(gap,Math.min(top,stageBox.height-tip.height-gap))}px`;
  };
  stage.addEventListener('pointermove',show);stage.addEventListener('pointerdown',show);stage.addEventListener('pointerleave',event=>{if(event.pointerType!=='touch')hide()});
}
function bindChartTimeframes(container,data){
  mountLightweightChart(container,data,container.querySelector('.price-chart')?.dataset.activeFrame);
  container.querySelectorAll('[data-chart-frame]:not(:disabled)').forEach(button=>button.onclick=()=>{container._chartCleanup?.();container.innerHTML=priceChart(data,button.dataset.chartFrame);bindChartTimeframes(container,data)})
}

function signalPanel(data,label){const side=data.signal.side;const market=location.pathname.split('/')[2]||'';const quote=market.startsWith('crypto_')?'USDT':'USD';const marketData=data.market||{};const liveTitle=market.startsWith('crypto_')?data.symbol.replace(/USDT$/,'/USDT')+' live price':data.display_name+' live price';const updated=data.last_updated?new Date(data.last_updated).toLocaleString():'—';const metrics=[['Probability',data.signal.probability==null?null:Math.round(data.signal.probability*100)+'%'],['Risk score',data.signal.risk_score==null?null:Math.round(data.signal.risk_score*100)+'/100'],['Stop loss',data.signal.stop_loss==null?null:number(data.signal.stop_loss)],['Take profit',data.signal.take_profit==null?null:number(data.signal.take_profit)],['Position size',data.signal.position_size_pct==null?null:(data.signal.position_size_pct*100).toFixed(1)+'%']].filter(x=>x[1]!=null);return `<section class="detail-grid signal-price-grid"><article class="panel signal-card"><p class="eyebrow">${label}</p><div class="decision ${side.includes('BUY')?'positive':side.includes('SELL')?'negative':''}">${safe(side)}</div><div class="live-price"><small>${safe(liveTitle)}</small><strong>${number(data.live_price??marketData.price)} <em>${quote}</em></strong><div><span class="${(data.change_24h??marketData.change_24h)>=0?'positive':'negative'}">24h ${pct(data.change_24h??marketData.change_24h)}</span><span>Volume ${money(data.volume??marketData.volume)}</span></div><p>Source: ${safe(data.source||marketData.source)} · Last updated: ${safe(updated)}</p></div><p>Confidence ${Math.round(data.signal.confidence*100)}%</p><div class="confidence-bar"><span style="width:${Math.round(data.signal.confidence*100)}%"></span></div>${metrics.map(x=>`<p><b>${x[0]}:</b> ${x[1]}</p>`).join('')}</article><article class="panel chart-card"><div class="chart-mount">${priceChart(data)}</div></article></section><section class="panel reasoning-panel"><p class="eyebrow">PLAIN-LANGUAGE REASONING</p><div class="plain-copy">${safe(data.plain_language_reason||data.signal.rationale)}</div></section>`}

function technicalPanels(data){const rows=data.timeframe_breakdown||[];const levels=data.key_levels||{};return `<section class="technical-grid"><article class="panel"><p class="eyebrow">MULTI-TIMEFRAME TECHNICALS</p>${rows.length?`<div class="table-wrap"><table class="market-table technical-table"><thead><tr><th>Timeframe</th><th>Trend</th><th>RSI</th><th>MACD</th><th>EMA bias</th><th>Score</th></tr></thead><tbody>${rows.map(r=>`<tr><td><b>${safe(r.timeframe)}</b></td><td class="${r.trend==='Bullish'?'positive':r.trend==='Bearish'?'negative':''}">${safe(r.trend)}</td><td>${number(r.rsi)}</td><td>${safe(r.macd)}</td><td>${number(r.ema_bias)}</td><td>${number(r.score)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="warning">Candle history is temporarily unavailable. The signal safely fell back to live-price rules.</p>'}</article><article class="panel"><p class="eyebrow">KEY LEVELS</p><div class="level-grid"><div><small>Support</small><strong>${number(levels.support)}</strong></div><div><small>Resistance</small><strong>${number(levels.resistance)}</strong></div><div><small>Stop loss</small><strong>${number(levels.stop_loss)}</strong></div><div><small>Take profit</small><strong>${number(levels.take_profit)}</strong></div></div></article></section><section class="panel why-panel"><p class="eyebrow">WHY THIS SIGNAL?</p><h2>${safe(data.trend_summary||'')}</h2><p>${safe(data.momentum_summary||'')}</p><p>${safe(data.volatility_summary||'')}</p><p class="plain-copy">${safe(data.plain_language_reason||data.signal.rationale)}</p></section>`}

async function assetPage(market,symbol){
  const intro='A fast multi-timeframe technical signal. Deep AI runs only when you request it.';
  app.innerHTML=`<a class="back" href="/scores/${market}" data-link>← AI rankings</a>${pageHeader('QUICK SIGNAL',safe(displaySymbol(symbol)),safe(intro))}<div class="loading-page"><div class="spinner"></div><p>Calculating Quick Signal…</p></div>`;
  try{const quick=await getJSON('/api/analyze/quick',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({market,venue:venueFor(market),symbol,equity:100000})});app.innerHTML=`<a class="back" href="/scores/${market}" data-link>← AI rankings</a>${pageHeader('QUICK SIGNAL',safe(quick.display_name||displaySymbol(symbol)),safe(quick.description||intro))}${quick.description?`<aside class="what-is-this panel"><b>What is this?</b> ${safe(quick.description)}</aside>`:''}<div id="quick-result">${signalPanel(quick,'QUICK SIGNAL · NO AI')}${technicalPanels(quick)}</div><section class="deep-area panel"><div><p class="eyebrow">OPTIONAL DEEP AI RESEARCH</p><h2>TradingAgents multi-agent analysis</h2><p class="source-note">This may take a few minutes. Results are cached for 20 minutes by market and symbol.</p></div><button id="deep-button" class="primary">Run Deep AI Research</button><div id="deep-status" aria-live="polite"></div><div id="deep-result"></div></section>${market==='options'?'<section id="options-chain" class="panel"><div class="spinner"></div> Loading option chain…</section>':market==='indian_indices'&&symbol==='BANKNIFTY'?'<section class="panel"><p class="eyebrow">OPTIONS PREPARATION</p><p>Bank Nifty options data provider not configured yet.</p></section>':''}`;bindChartTimeframes(document.getElementById('quick-result').querySelector('.chart-mount'),quick);document.getElementById('deep-button').onclick=()=>runDeep(market,symbol,false);if(market==='options')loadOptions(symbol)}catch(error){errorView(error)}
}

async function runDeep(market,symbol,refresh=false){
  const key=`${market}:${symbol.toUpperCase()}`,button=document.getElementById('deep-button');
  button.disabled=true;
  try{let jobId=deepJobKeys.get(key)||sessionStorage.getItem(`deep-job:${key}`);if(!jobId||refresh){const job=await getJSON('/api/analyze/deep',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({market,venue:venueFor(market),symbol,equity:100000,refresh})});jobId=job.job_id;deepJobKeys.set(key,jobId);sessionStorage.setItem(`deep-job:${key}`,jobId);await handleDeepStatus(job,market,symbol,key);if(['queued','running'].includes(job.status))pollDeep(jobId,market,symbol,key,Date.now());return}await pollDeep(jobId,market,symbol,key,Date.now())}catch(error){document.getElementById('deep-status').innerHTML=`<p class="ai-error"><b>Deep AI error:</b> ${safe(error.message)} Quick Signal remains available above.</p>`;button.disabled=false}
}

async function pollDeep(jobId,market,symbol,key,waitStarted){
  const job=await getJSON(`/api/analyze/deep/status/${encodeURIComponent(jobId)}`);await handleDeepStatus(job,market,symbol,key);
  if(['queued','running'].includes(job.status)){if(Date.now()-waitStarted>=95000){showBackgroundWait(jobId,market,symbol,key);return}setTimeout(()=>pollDeep(jobId,market,symbol,key,waitStarted).catch(error=>showPollError(error,jobId,market,symbol,key)),2000)}
}

function runningMarkup(job){return `<div class="research-running"><div class="spinner"></div><div><p><b>${safe(job.progress_message)}</b></p><p>Step ${job.progress_step||1} of 5 · ${number(job.elapsed_seconds)}s elapsed</p><p>You can keep using Quick Signal while Deep AI runs.</p></div></div>`}

function deepInsight(data){
  const deep=data.signal||{},quick=data.quick_signal?.signal||{},deepSide=String(deep.side||'—').toUpperCase(),quickSide=String(quick.side||'—').toUpperCase(),agrees=deepSide===quickSide;
  const confidence=deep.confidence==null?'Not provided':`${Math.round(deep.confidence*100)}%`,reason=data.plain_language_reason||deep.rationale||'No additional reasoning was provided.';
  const riskNotes=data.risk_notes||`Risk score ${deep.risk_score==null?'not provided':Math.round(deep.risk_score*100)+'/100'}${deep.stop_loss==null?'':` · Stop ${number(deep.stop_loss)}`}${deep.take_profit==null?'':` · Target ${number(deep.take_profit)}`}`;
  const watch=data.what_to_watch_next||'Watch the main chart’s support, resistance, momentum, and volume for confirmation before acting.';
  return `<article class="deep-insight"><div class="deep-comparison ${agrees?'agrees':'differs'}"><strong>Deep AI ${agrees?'agrees with':'differs from'} Quick Signal</strong>${agrees?'':`<span>Quick Signal: <b>${safe(quickSide)}</b> · Deep AI: <b>${safe(deepSide)}</b></span>`}</div><div class="deep-decision"><div><small>Deep AI decision</small><strong class="decision ${deepSide.includes('BUY')?'positive':deepSide.includes('SELL')?'negative':''}">${safe(deepSide)}</strong></div><div><small>Confidence</small><strong>${safe(confidence)}</strong></div></div>${!agrees?`<section><h3>Reason for difference</h3><p>${safe(data.difference_reason||reason)}</p></section>`:''}<section><h3>Deep AI reasoning</h3><p class="plain-copy">${safe(reason)}</p></section><div class="deep-notes"><section><h3>Risk notes</h3><p>${safe(riskNotes)}</p></section><section><h3>What to watch next</h3><p>${safe(watch)}</p></section></div></article>`;
}

async function handleDeepStatus(job,market,symbol,key){const status=document.getElementById('deep-status'),button=document.getElementById('deep-button');if(['queued','running'].includes(job.status)){status.innerHTML=runningMarkup(job);button.textContent='Check Deep AI status';button.disabled=false;button.onclick=()=>pollDeep(job.job_id,market,symbol,key,Date.now());return}if(job.status==='completed'){const data=job.result;status.innerHTML=`<div class="deep-meta">Deep AI completed${job.cached?' · cached':''} <button id="refresh-deep" class="ghost">Refresh Deep AI</button></div>`;document.getElementById('deep-result').innerHTML=deepInsight(data);button.textContent='Refresh Deep AI';button.disabled=false;button.onclick=()=>runDeep(market,symbol,true);document.getElementById('refresh-deep').onclick=()=>runDeep(market,symbol,true);return}const fallback=job.fallback_result;status.innerHTML=`<p class="ai-error">${safe(job.user_friendly_error)}</p><button id="restart-deep" class="ghost">Start new Deep AI run</button>`;if(fallback){document.getElementById('deep-result').innerHTML='<p class="background-notice"><b>Deep AI fallback used.</b> The main Quick Signal above remains the source of this result.</p>'}button.textContent='Start new Deep AI run';button.disabled=false;button.onclick=()=>runDeep(market,symbol,true);document.getElementById('restart-deep').onclick=()=>runDeep(market,symbol,true)}

function showBackgroundWait(jobId,market,symbol,key){const status=document.getElementById('deep-status'),button=document.getElementById('deep-button');status.innerHTML='<p class="background-notice">Deep AI is still running in the background. Quick Signal remains available.</p><div class="deep-actions"><button id="check-deep" class="ghost">Check status</button><button id="continue-deep" class="ghost">Continue waiting</button><button id="fallback-deep" class="ghost">Use Quick Signal fallback</button></div>';button.textContent='Check Deep AI status';button.disabled=false;button.onclick=()=>pollDeep(jobId,market,symbol,key,Date.now());document.getElementById('check-deep').onclick=button.onclick;document.getElementById('continue-deep').onclick=()=>pollDeep(jobId,market,symbol,key,Date.now());document.getElementById('fallback-deep').onclick=()=>{status.innerHTML='<p class="background-notice">Quick Signal remains available above. Deep AI continues in the background.</p>'}}
function showPollError(error,jobId,market,symbol,key){document.getElementById('deep-status').innerHTML=`<p class="ai-error">Could not check Deep AI status: ${safe(error.message)}</p><button id="check-deep" class="ghost">Check status</button>`;document.getElementById('check-deep').onclick=()=>pollDeep(jobId,market,symbol,key,Date.now())}

async function loadOptions(symbol){const target=document.getElementById('options-chain');try{const d=await getJSON(`/api/options/${encodeURIComponent(symbol)}`);const fields=[['Put/Call Ratio',d.put_call_ratio],['Max Pain',d.max_pain]].filter(x=>x[1]!=null);const columns=[['option_type','Type'],['strike','Strike'],['open_interest','Open Interest'],['iv','IV'],['delta','Delta'],['gamma','Gamma'],['theta','Theta'],['vega','Vega']].filter(([key])=>d.contracts.some(r=>r[key]!=null&&r[key]!==''));target.innerHTML=`<p class="eyebrow">OPENBB OPTION CHAIN</p><div>${fields.map(x=>`<b>${x[0]}:</b> ${number(x[1])} `).join('')}</div>${d.expiries.length?`<label>Expiry <select id="expiry">${d.expiries.map(x=>`<option>${safe(x)}</option>`).join('')}</select></label>`:''}${d.strikes.length?`<label>Strike <select>${d.strikes.map(x=>`<option>${number(x)}</option>`).join('')}</select></label>`:''}<div class="table-wrap"><table class="market-table"><thead><tr>${columns.map(x=>`<th>${x[1]}</th>`).join('')}</tr></thead><tbody>${d.contracts.map(r=>`<tr>${columns.map(([key])=>`<td>${key==='option_type'?safe(r[key]):number(r[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`}catch(e){target.remove()}}

function bankOptionRows(rows){return rows.map(r=>`<tr><td><b>${safe(r.option_type)}</b></td><td>${number(r.strike)}</td><td>${safe(r.moneyness)}</td><td>${number(r.last_price)}</td><td class="${r.change>=0?'positive':'negative'}">${pct(r.change)}</td><td>${number(r.volume)}</td><td>${number(r.open_interest)}</td><td>${number(r.implied_volatility)}</td><td>${number(r.bid)} / ${number(r.ask)}</td><td><b>${safe(r.score.signal)}</b><br><small>${Math.round(r.score.confidence*100)}% · risk ${r.score.risk_score}</small></td></tr>`).join('')}

async function bankNiftyOptionsPage(){
  app.innerHTML='<div class="loading-page"><div class="spinner"></div><p>Loading the real Bank Nifty option chain…</p></div>';
  try{const data=await getJSON('/api/banknifty-options');if(!data.available){app.innerHTML=`<a class="back" href="/" data-link>← All markets</a>${pageHeader('RESEARCH ONLY','Bank Nifty Options','CE/PE strikes, expiry, OI and option signals')}<div class="empty-state panel"><h2>${safe(data.message)}</h2><div class="provider-attempts"><p><b>OpenBB tried:</b> ${data.provider_attempts?.openbb?'Yes':'No'}</p><p><b>NSE fallback tried:</b> ${data.provider_attempts?.nse_fallback?'Yes':'No'}</p><p><b>Failure category:</b> ${safe(data.failure_category||'provider unavailable')}</p></div><p>No option rows are generated when a real chain is unavailable. Research only; no live options order placement.</p></div>`;return}
    app.innerHTML=`<a class="back" href="/" data-link>← All markets</a>${pageHeader('RESEARCH ONLY','Bank Nifty Options','CE/PE strikes, expiry, OI and option signals')}<section class="option-reference panel"><div><small>BANK NIFTY live reference price</small><strong>${number(data.underlying_price)}</strong></div><div><small>ATM strike</small><strong>${number(data.atm_strike)}</strong></div><label>Expiry <select id="option-expiry"><option value="">All</option>${data.expiries.map(x=>`<option value="${safe(x)}">${safe(x)}</option>`).join('')}</select></label></section><div class="option-filters" id="option-types">${['ALL','CE','PE'].map(x=>`<button class="ghost${x==='ALL'?' active':''}" data-filter="${x}">${x}</button>`).join('')}</div><div class="option-filters" id="option-money">${['ALL','ITM','ATM','OTM'].map(x=>`<button class="ghost${x==='ALL'?' active':''}" data-filter="${x}">${x}</button>`).join('')}</div><div class="table-wrap"><table class="market-table"><thead><tr><th>Type</th><th>Strike</th><th>Moneyness</th><th>Last</th><th>Change</th><th>Volume</th><th>Open interest</th><th>IV</th><th>Bid / Ask</th><th>Option signal</th></tr></thead><tbody id="bank-option-body">${bankOptionRows(data.contracts)}</tbody></table></div><p class="source-note">Source: ${safe(data.source)} · Research only · No live options order placement.</p>`;
    let typeFilter='ALL',moneyFilter='ALL';const draw=()=>{const expiry=document.getElementById('option-expiry').value;document.getElementById('bank-option-body').innerHTML=bankOptionRows(data.contracts.filter(r=>(!expiry||r.expiry===expiry)&&(typeFilter==='ALL'||r.option_type===typeFilter)&&(moneyFilter==='ALL'||r.moneyness===moneyFilter)))};document.getElementById('option-expiry').addEventListener('change',draw);[['option-types','type'],['option-money','money']].forEach(([id,kind])=>document.getElementById(id).addEventListener('click',e=>{if(!e.target.dataset.filter)return;if(kind==='type')typeFilter=e.target.dataset.filter;else moneyFilter=e.target.dataset.filter;document.querySelectorAll(`#${id} button`).forEach(b=>b.classList.toggle('active',b.dataset.filter===e.target.dataset.filter));draw()}))
  }catch(error){errorView(error)}
}

function render(){const parts=location.pathname.split('/').filter(Boolean);if(!parts.length)return home();if(parts[0]==='market'&&parts[1]==='banknifty_options')return bankNiftyOptionsPage();if(parts[0]==='market'&&parts[1])return marketPage(parts[1]);if(parts[0]==='scores'&&parts[1])return scoresPage(parts[1]);if(parts[0]==='asset'&&parts[1]&&parts[2])return assetPage(parts[1],decodeURIComponent(parts.slice(2).join('/')));return home();}

let installPrompt;window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installPrompt=e});document.getElementById('install').addEventListener('click',async()=>{if(installPrompt){installPrompt.prompt();await installPrompt.userChoice;installPrompt=null}else alert('In Chrome, open the menu and choose “Cast, save and share” → “Install page as app”.')});if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js');render();

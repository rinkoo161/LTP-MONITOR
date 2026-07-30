// Drives the REAL chart functions lifted verbatim out of dashboard.html
// against stubs (the LWC CDN is unreachable from the build sandbox, so
// no browser render is possible). Covers: per-symbol reset, the
// fit-with-empty-panes bug (2026-07-26: fitContent() on an empty pane
// propagated a degenerate range back onto the main chart, blanking it
// after every symbol/interval switch), vertical candle zoom, price-scale
// width equalization, levels toggle, and whitespace mapping.
let calls=[];
function mkSeries(tag){
  return {
    setData(d){calls.push([tag,"setData",Array.isArray(d)?d.length:-1]);},
    setMarkers(m){calls.push([tag,"setMarkers",m.length]);},
    createPriceLine(o){calls.push([tag,"priceLine",o.price]);return o;},
    removePriceLine(){},
  };
}
function mkChart(tag, hasData, axisWidth){
  const c={
    _tag:tag, _range:null, _fitCalls:0, _axisW:axisWidth, _minW:null,
    _autoScaleSet:0,
    timeScale(){ const self=c; return {
      fitContent(){ self._fitCalls++;
        self._range = hasData? {from:0,to:99} : {from:NaN,to:NaN}; },
      getVisibleLogicalRange(){ return self._range; },
      setVisibleLogicalRange(r){ self._range=r;
        calls.push([self._tag,"setRange",r&&r.to]); },
    };},
    priceScale(){ const self=c; return {
      width(){ return self._axisW; },
      applyOptions(o){ if("minimumWidth" in o) self._minW=o.minimumWidth;
                       if(o.autoScale) self._autoScaleSet++; },
    };},
  };
  return c;
}
let lwSeries=mkSeries("candles"), lwZigzagSeries=mkSeries("zigzag"),
    lwVolumeSeries=mkSeries("volume");
let lwOverlaySeries={};
["ema20","ema50","supertrend","bb_upper","bb_lower","atr_band_upper",
 "atr_band_lower","vol_band_upper","vol_band_lower","support_zone_upper",
 "support_zone_lower"].forEach(k=>{lwOverlaySeries[k]=mkSeries("ov:"+k);});
let lwPanes={
  macd:{series:{hist:mkSeries("pn:mh"),line:mkSeries("pn:ml"),signal:mkSeries("pn:ms")},dataByTime:{a:1}},
  rsi:{series:{line:mkSeries("pn:rsi")},dataByTime:{a:1}},
  stoch:{series:{k:mkSeries("pn:k"),d:mkSeries("pn:d")},dataByTime:{a:1}},
  atr:{series:{line:mkSeries("pn:atr")},dataByTime:{a:1}},
};
let lwChart=mkChart("main", true, 74);
let paneCharts=[mkChart("macd",false,44),mkChart("rsi",false,40),
                mkChart("stoch",false,48),mkChart("atr",false,32)];
lwPanes.macd.chart=paneCharts[0]; lwPanes.rsi.chart=paneCharts[1];
lwPanes.stoch.chart=paneCharts[2]; lwPanes.atr.chart=paneCharts[3];
let lwAllCharts=[lwChart].concat(paneCharts);
let lwSyncingRange=false;
let lwCandleByTime={x:1}, lwTradeAndFlagMarkers=[1], lwZigzagMarkers=[2];
let lwSignalMarkers=[], lwSignalsVisible=true, lwZigzagVisible=true;
let lwPriceLines=[];
let lwShowLevelLines=true, lwLastLevels=null;
const els={lwLevelsDetail:{innerHTML:"x"},lwIndicatorNote:{textContent:"x",style:{}},
           lwOhlcReadout:{textContent:"x"},lwChartContainer:{style:{}}};
const document={getElementById:id=>els[id]||null};
const IST_OFFSET_SECONDS=19800;
function toLwTime(unixSeconds){
  // Lightweight Charts renders a UNIX timestamp as UTC wall-clock time
  // with no built-in fixed-timezone option in this version — but this
  // entire dashboard is IST. Shifting the value we hand to the chart
  // by +5:30 makes its UTC display show the correct IST digits. Bug
  // found live 2026-07-25 (chart showed 21:48 when it was 03:21 AM
  // IST — 21:48 UTC the previous day, confirming this exact mismatch).
  return unixSeconds + IST_OFFSET_SECONDS;
}

function resetLwChartData(){
  lwChartHasData=new Map();
  lwRangeLockUntil=Date.now()+1500;   // absorb async stale range events from the teardown
  if(lwSeries){ lwSeries.setData([]); lwSeries.setMarkers([]); }
  lwCandleByTime={};
  lwTradeAndFlagMarkers=[];
  lwZigzagMarkers=[];
  if(lwZigzagSeries) lwZigzagSeries.setData([]);
  Object.keys(lwOverlaySeries||{}).forEach(function(k){
    try{ lwOverlaySeries[k].setData([]); }catch(e){}
  });
  Object.keys(lwPanes||{}).forEach(function(k){
    const pane=lwPanes[k];
    Object.keys(pane.series||{}).forEach(function(sk){
      try{ pane.series[sk].setData([]); }catch(e){}
    });
    pane.dataByTime={};
  });
  if(lwVolumeSeries) lwVolumeSeries.setData([]);
  lwLastLevels=null;
  clearLwPriceLines();
  const det=document.getElementById("lwLevelsDetail");
  if(det) det.innerHTML='<div class="reasons">Loading...</div>';
  const note=document.getElementById("lwIndicatorNote");
  if(note){ note.textContent="Feature #7 indicators \u2014 refresh every ~30s";
            note.style.color=""; }
  const ohlc=document.getElementById("lwOhlcReadout");
  if(ohlc) ohlc.textContent="";
}

function fitLwChart(){
  if(!lwChart) return;
  lwVZoom=1;                    // vertical zoom back to neutral
  lwSyncingRange=true;
  try{ lwChart.timeScale().fitContent(); }catch(e){}
  let range=null;
  try{ range=lwChart.timeScale().getVisibleLogicalRange(); }catch(e){}
  (lwAllCharts||[]).forEach(function(ch){
    if(!ch) return;
    if(ch!==lwChart && range){
      try{ ch.timeScale().setVisibleLogicalRange(range); }catch(e){}
    }
    try{ ch.priceScale("right").applyOptions({autoScale:true}); }catch(e){}
  });
  lwSyncingRange=false;
  syncLwPriceScaleWidths();
}

function lwVerticalZoom(dir){
  lwVZoom=Math.max(0.25, Math.min(8, lwVZoom*(dir>0?1.25:0.8)));
  // Force an autoscale recalculation so the provider is re-consulted
  // immediately rather than on the next data update.
  try{ lwChart.priceScale("right").applyOptions({autoScale:true}); }catch(e){}
}

function resizeLwChart(delta){
  lwChartHeight=Math.max(200, Math.min(900, lwChartHeight+delta));
  const c=document.getElementById("lwChartContainer");
  if(c) c.style.height=lwChartHeight+"px";
  if(lwChart) lwChart.applyOptions({height:lwChartHeight});
}

function toggleLwPane(key, visible){
  const el=document.getElementById(lwPaneContainers[key]);
  if(el) el.style.display=visible?"":"none";
  if(visible && lwPanes[key] && lwChart){
    try{
      const r=lwChart.timeScale().getVisibleLogicalRange();
      if(r) lwPanes[key].chart.timeScale().setVisibleLogicalRange(r);
    }catch(e){}
    syncLwPriceScaleWidths();
  }
}

function syncLwPriceScaleWidths(){
  const charts=(lwAllCharts&&lwAllCharts.length)?lwAllCharts:[lwChart];
  let w=0;
  charts.forEach(function(ch){
    if(!ch) return;
    try{ w=Math.max(w, ch.priceScale("right").width()); }catch(e){}
  });
  if(!w) return;
  charts.forEach(function(ch){
    if(!ch) return;
    try{ ch.priceScale("right").applyOptions({minimumWidth:w}); }catch(e){}
  });
}

function toggleLwLevels(show){
  lwShowLevelLines=show;
  if(show) drawLwLevelLines(lwLastLevels);
  else clearLwPriceLines();
}

function drawLwLevelLines(lv){
  clearLwPriceLines();
  if(!lv || !lwSeries || !lwShowLevelLines) return;
  function lineTitle(l){
    let t=l.label;
    if(l.source_label) t+=" "+l.source_label;
    if(l.strength!=null) t+=" "+l.strength+"%";
    return t;
  }
  (lv.R||[]).forEach(function(l){
    lwPriceLines.push(lwSeries.createPriceLine({
      price:l.level, color:"#f85149", lineWidth:1, lineStyle:2,
      axisLabelVisible:true, title:lineTitle(l)}));
  });
  (lv.S||[]).forEach(function(l){
    lwPriceLines.push(lwSeries.createPriceLine({
      price:l.level, color:"#3fb950", lineWidth:1, lineStyle:2,
      axisLabelVisible:true, title:lineTitle(l)}));
  });
}

function clearLwPriceLines(){
  lwPriceLines.forEach(function(pl){ try{lwSeries.removePriceLine(pl);}catch(e){} });
  lwPriceLines=[];
}

function lwToSeriesData(arr, withColor){
  return (arr||[]).map(function(p){
    if(p.value==null) return {time:toLwTime(p.time)};      // whitespace
    const pt={time:toLwTime(p.time), value:p.value};
    if(withColor && p.color) pt.color=p.color;
    return pt;
  });
}

function lwToDataByTime(arr){
  const out={};
  (arr||[]).forEach(function(p){
    if(p.value!=null) out[toLwTime(p.time)]=p.value;
  });
  return out;
}

function lwRedrawMarkers(){
  // v51 — three marker layers through ONE funnel, per the S7 spec's
  // "known trap": setMarkers() REPLACES the whole set, so every layer
  // must merge here, and the merged set MUST be sorted ascending by
  // time — three independently-built arrays are not in order and LWC
  // requires it (previously trade_markers + capped flag markers were
  // concatenated unsorted, a latent bug this fixes for the existing
  // layers too, not only the new one).
  if(!lwSeries) return;
  const all=[].concat(
    lwTradeAndFlagMarkers||[],
    lwZigzagVisible ? (lwZigzagMarkers||[]) : [],
    lwSignalsVisible ? (lwSignalMarkers||[]) : []);
  all.sort(function(a,b){ return a.time-b.time; });
  lwSeries.setMarkers(all);
}

let fail=0;
function chk(l,c,d){console.log((c?"  PASS  ":"  FAIL  ")+l+(d?"   ["+d+"]":""));if(!c)fail++;}

console.log("1) reset clears every per-symbol artefact");
calls=[]; lwLastLevels={R:[{level:1}]};
resetLwChartData();
const cleared=new Set(calls.filter(c=>c[1]==="setData"&&c[2]===0).map(c=>c[0]));
const expect=["candles","zigzag","volume",
  ...Object.keys(lwOverlaySeries).map(k=>"ov:"+k),
  "pn:mh","pn:ml","pn:ms","pn:rsi","pn:k","pn:d","pn:atr"];
const miss=expect.filter(e=>!cleared.has(e));
chk("all "+expect.length+" series emptied",miss.length===0,miss.join(","));
chk("levels forgotten + markers cleared",
    lwLastLevels===null && lwTradeAndFlagMarkers.length===0);

console.log("\n2) fit with EMPTY panes must not blank the main chart");
calls=[]; lwVZoom=3;   // also verify fit resets vertical zoom
fitLwChart();
chk("main fitContent called once", lwChart._fitCalls===1);
chk("panes NEVER fitContent'd (empty panes emit degenerate ranges)",
    paneCharts.every(p=>p._fitCalls===0),
    paneCharts.map(p=>p._tag+":"+p._fitCalls).join(" "));
chk("main's range pushed onto every pane",
    paneCharts.every(p=>p._range && p._range.to===99));
chk("main chart's own range untouched by any pane",
    lwChart._range && lwChart._range.to===99);
chk("autoScale restored on all 5 charts",
    lwAllCharts.every(c=>c._autoScaleSet>=1));
chk("vertical zoom reset to neutral", lwVZoom===1);

console.log("\n3) vertical zoom (\u25b2\u25bc) scales candles, not the pane");
lwVerticalZoom(1); const z1=lwVZoom;
lwVerticalZoom(1); const z2=lwVZoom;
chk("zoom-in multiplies the factor", z1===1.25 && z2>z1, "z="+z2);
for(let i=0;i<40;i++) lwVerticalZoom(1);
chk("clamped at 8x", lwVZoom===8, "z="+lwVZoom);
for(let i=0;i<80;i++) lwVerticalZoom(-1);
chk("clamped at 0.25x", lwVZoom===0.25, "z="+lwVZoom);
// the provider math itself (inline in initLwChart, replicated here from
// the same formula to pin the contract):
lwVZoom=2;
const orig=()=>({priceRange:{minValue:100,maxValue:200},margins:null});
const r=(function(original){const rr=original();
  const mid=(rr.priceRange.minValue+rr.priceRange.maxValue)/2;
  const half=(rr.priceRange.maxValue-rr.priceRange.minValue)/2/lwVZoom;
  return {priceRange:{minValue:mid-half,maxValue:mid+half}};})(orig);
chk("2x zoom halves the range around the midpoint",
    r.priceRange.minValue===125 && r.priceRange.maxValue===175,
    JSON.stringify(r.priceRange));
lwVZoom=1;

console.log("\n4) price-scale width equalization (crosshair x-alignment)");
syncLwPriceScaleWidths();
chk("every pane forced to the widest axis (74px)",
    lwAllCharts.every(c=>c._minW===74),
    lwAllCharts.map(c=>c._tag+":"+c._minW).join(" "));

console.log("\n5) levels toggle + whitespace mapping (regression)");
const lv={R:[{level:23823,label:"R2"}],S:[{level:23700,label:"S1"}]};
lwPriceLines=[];calls=[];
drawLwLevelLines(lv);
chk("lines drawn when enabled",calls.filter(c=>c[1]==="priceLine").length===2);
lwLastLevels=lv; toggleLwLevels(false);
chk("hidden when off",lwPriceLines.length===0);
toggleLwLevels(true);
const pts=lwToSeriesData([{time:1},{time:2,value:5}]);
chk("whitespace point has NO value key", !("value" in pts[0]) && pts[1].value===5);
const map=lwToDataByTime([{time:1},{time:2,value:7}]);
chk("whitespace not registered for crosshair",
    map[toLwTime(1)]===undefined && map[toLwTime(2)]===7);


console.log("\n6) marker funnel: three layers merged + SORTED by time");
calls=[];
let sent=null;
lwSeries={setData(){},setMarkers(m){sent=m;},createPriceLine(o){return o;},removePriceLine(){}};
lwTradeAndFlagMarkers=[{time:30,text:"exit"},{time:10,text:"entry"}];
lwZigzagMarkers=[{time:20,text:"HH"}];
lwSignalMarkers=[{time:15,text:"S7"},{time:5,text:"S7b"}];
lwZigzagVisible=true; lwSignalsVisible=true;
lwRedrawMarkers();
chk("all three layers present", sent && sent.length===5, sent&&sent.length);
chk("merged set sorted ascending by time",
    sent && sent.every((m,i)=>i===0||sent[i-1].time<=m.time),
    sent && sent.map(m=>m.time).join(","));
lwSignalsVisible=false; lwRedrawMarkers();
chk("hiding the S7 layer removes only its markers",
    sent.length===3 && !sent.some(m=>String(m.text).indexOf("S7")===0),
    sent.map(m=>m.time).join(","));

console.log("\n"+"=".repeat(58));
if(fail){console.log("FAIL: "+fail);process.exit(1);}
console.log("PASS \u2014 all checks");

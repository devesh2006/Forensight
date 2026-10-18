import { api, query } from "./api.js?v=20261019-ingest";
import { refreshSystemStatus, registerStatusRetry, setAnalysisStatus, startSystemStatus } from "./system-status.js?v=20261019-ingest";
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
const palette = ["#62c5ad","#a990e6","#e2ad61","#70b0e7","#df8585","#69c2d0","#d28cc1","#a9be71"];
const parseTime = t => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(t) ? t : t + "+05:30");
const caseTimezone = () => statsData?.timezone || "Asia/Kolkata";
const zoneLabel = () => caseTimezone()==="Asia/Kolkata"?"IST":caseTimezone();
const fmtTime = t => parseTime(t).toLocaleString("en-IN",{timeZone:caseTimezone(),day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",hour12:false});
const day = t => parseTime(t).toLocaleDateString("en-IN",{timeZone:caseTimezone(),weekday:"long",day:"2-digit",month:"long",year:"numeric"});
let offset=0, timelineOffset=0, incidentOnly=false, statsData=null, locationData=[], geoAnalysis=null, entityData=[], timelineData=[], graphData={nodes:[],edges:[]}, requestSeq=0;
function setTheme(theme){
  document.documentElement.dataset.theme=theme;
  localStorage.setItem("forensight-theme",theme);
  if(statsData){if($("#graph").classList.contains("active"))renderGraph();if($("#map").classList.contains("active"))renderMap()}
  const next=theme==="light"?"dark":"light";
  $("#themeToggle").innerHTML=(next==="light"?"☼":"☾")+' <span>'+next[0].toUpperCase()+next.slice(1)+"</span>";
  $("#themeToggle").setAttribute("aria-label","Switch to "+next+" theme");
}
setTheme(localStorage.getItem("forensight-theme")==="light"?"light":"dark");
$("#themeToggle").onclick=()=>setTheme(document.documentElement.dataset.theme==="light"?"dark":"light");
$("#uploadBtn").onclick=()=>$("#reportFile").click();
let lastUploadFile=null;
registerStatusRetry(async()=>{
  if(!lastUploadFile)return;
  setAnalysisStatus("running","Refreshing analysis from evidence already stored in the active case.");
  try{
    await refreshSystemStatus();
    if(!await loadDashboard())throw new Error("Evidence was imported, but case analysis could not be refreshed.");
    if($("#graph").classList.contains("active"))await loadGraph();
    if($("#map").classList.contains("active"))await loadMap();
    setAnalysisStatus("complete","Evidence ingestion and case analysis completed successfully.");
    notify("Analysis refreshed from evidence already stored in this case.",false);
  }catch(error){setAnalysisStatus("failed","Evidence ingestion completed, but case analysis failed: "+error.message)}
});
async function submitImport(file,caseId){
  const form=new FormData();form.append("file",file,file.name);
  const queued=await api("/api/cases/"+encodeURIComponent(caseId)+"/imports",{method:"POST",body:form});
  for(let attempt=0;attempt<1200;attempt++){
    const job=await api("/api/imports/"+encodeURIComponent(queued.import_id));
    if(job.status==="complete")return job.manifest;
    if(job.status==="failed")throw new Error(job.errors?.[0]?.error||"Evidence import failed.");
    const counts=job.files_total?`${job.files_processed}/${job.files_total} files`:"detecting files";
    const records=job.records_total?` · ${job.records_processed}/${job.records_total} records`:"";
    setAnalysisStatus("running",`${job.current_stage||job.status} · ${counts}${records}`);
    await new Promise(resolve=>setTimeout(resolve,300));
  }
  throw new Error("Import is still running. Check the system status and retry analysis after it completes.");
}
$("#reportFile").onchange=async event=>{
  const file=event.target.files?.[0];if(!file)return;
  if(file.size>50*1024*1024){notify("Dataset exceeds the 50 MB upload limit.");event.target.value="";return}
  const button=$("#uploadBtn");button.disabled=true;button.innerHTML="↑ <span>Uploading…</span>";
  try{
    setAnalysisStatus("importing","Reading and uploading "+file.name+".");
    lastUploadFile=file;
    setAnalysisStatus("validating","The backend is validating the uploaded evidence.");
    const caseId=$("#caseSwitcher").value;
    const result=await submitImport(file,caseId);
    await refreshSystemStatus();
    setAnalysisStatus("running","Imported "+result.records_imported+" records. Refreshing case analysis.");
    if(!await loadDashboard())throw new Error("Evidence ingestion succeeded, but case analysis could not be refreshed.");
    showImportResult(result);await refreshCaseSwitcher(result.case_id);
    if($("#evidence").classList.contains("active"))renderEvidence();
    if($("#timeline").classList.contains("active"))renderTimeline();
    if($("#graph").classList.contains("active"))loadGraph();
    if($("#map").classList.contains("active"))loadMap();
    setAnalysisStatus("complete","Evidence ingestion and case analysis completed successfully.");
    notify("Imported "+result.records_imported+" evidence record(s); "+result.records_failed+" record(s) need review.",result.records_failed>0);
  }catch(error){setAnalysisStatus("failed","Report upload or case analysis failed: "+error.message);notify("Report upload failed: "+error.message)}
  finally{button.disabled=false;button.innerHTML="↑ <span>Upload report</span>";event.target.value=""}
};
const errorBox = msg => '<div class="empty api-error" role="status">'+esc(msg)+'</div>';
function notify(message, error=true) { const n=$("#notice"); n.textContent=message; n.classList.remove("hidden"); n.classList.toggle("error",error); }
function clearNotice(){ $("#notice").classList.add("hidden"); }
function showImportResult(manifest){
  const panel=$("#importResult");panel.classList.remove("hidden");
  const detected=manifest.by_type_detected||manifest.by_type||{};
  const rows=Object.entries(detected).map(([type,total])=>{const imported=(manifest.by_type||{})[type]||0;return '<div class="import-type-row"><b>'+esc(type.toUpperCase())+'</b><span>'+imported+' / '+total+' '+(imported===total?'✓':'⚠')+'</span></div>'}).join("");
  const errors=[...(manifest.errors||[]),...(manifest.warnings||[])],files=(manifest.files||[]).map(f=>'<div class="import-file-row"><span>'+esc(f.file)+'</span><b>'+esc(String(f.parsed??0))+' / '+esc(String(f.detected??0))+'</b><small>'+esc(f.kind||'evidence')+'</small></div>').join("");
  const skipped=(manifest.skipped_files||[]).map(f=>'<div class="import-file-row skipped"><span>'+esc(f.file)+'</span><b>Unsupported file — skipped</b><small>'+esc(f.reason)+'</small></div>').join("");
  panel.innerHTML='<div class="import-result-head"><div><div class="panel-kicker">DATASET MANIFEST</div><h2>EVIDENCE IMPORT '+(manifest.records_failed?'REVIEW':'COMPLETE')+'</h2><p>'+esc(manifest.case_id)+' · '+esc(manifest.case_name||"")+'</p></div><button id="closeImportResult" class="outline-btn">Close</button></div><div class="import-summary"><div><span>FILES DISCOVERED</span><b>'+manifest.files_discovered+'</b></div><div><span>FILES PROCESSED</span><b>'+manifest.files_processed+'</b></div><div><span>RECORDS DETECTED</span><b>'+manifest.records_detected+'</b></div><div><span>RECORDS IMPORTED</span><b>'+manifest.records_imported+'</b></div><div><span>FAILED</span><b>'+manifest.records_failed+'</b></div></div><div class="import-type-list">'+(rows||'<div class="empty">Metadata only; no evidence records.</div>')+'</div><details class="import-files"><summary>Show files read ('+((manifest.files||[]).length+(manifest.skipped_files||[]).length)+')</summary>'+files+skipped+'</details>'+(errors.length?'<details class="import-errors"><summary>View '+errors.length+' warning/error item(s)</summary><pre>'+esc(JSON.stringify(errors,null,2))+'</pre></details>':'')+'<button id="continueImport" class="upload-btn">Continue to analysis →</button>';
  $("#closeImportResult").onclick=()=>panel.classList.add("hidden");
  $("#continueImport").onclick=()=>{panel.classList.add("hidden");go("dashboard")};
}
async function refreshCaseSwitcher(selectCase){
  try{const result=await api("/api/cases");const selector=$("#caseSwitcher");selector.innerHTML=(result.cases||[]).map(c=>'<option value="'+esc(c.case_id)+'">'+esc(c.name)+' · '+c.evidence_count+'</option>').join("");
    if(selectCase||result.active_case)selector.value=selectCase||result.active_case;
  }catch(error){console.warn("Could not load cases",error)}
}
function go(page) {
  $$(".page").forEach(x=>x.classList.toggle("active",x.id===page));
  $$(".nav-item").forEach(x=>x.classList.toggle("active",x.dataset.page===page));
  history.replaceState(null,"","#"+page);
  if(page==="evidence") renderEvidence();
  if(page==="timeline") renderTimeline();
  if(page==="graph") loadGraph();
  if(page==="map") loadMap();
  window.scrollTo(0,0);
}
$$(".nav-item").forEach(b=>b.onclick=()=>go(b.dataset.page));
$$("[data-go]").forEach(b=>b.onclick=()=>go(b.dataset.go));
$(".brand").onclick=e=>{e.preventDefault();go("dashboard")};
$("#loadBtn").onclick=()=>loadDashboard();
$("#caseSwitcher").onchange=async()=>{
  const caseId=$("#caseSwitcher").value;if(!caseId)return;
  try{await api("/api/cases/"+encodeURIComponent(caseId)+"/activate",{method:"POST"});await refreshSystemStatus();await loadDashboard();renderEvidence();timelineOffset=0;
    if($("#timeline").classList.contains("active"))renderTimeline();if($("#graph").classList.contains("active"))loadGraph();if($("#map").classList.contains("active"))loadMap();
  }catch(error){notify("Could not switch case: "+error.message)}
};
function countType(type){return statsData?.by_type?.[type]||0}
function showStats(s){
  statsData=s;
  $("#loadBtn").innerHTML='✓ <span>CASE DATA CONNECTED</span>';
  const incidentDate=parseTime(s.incident);
  const incidentWindow=s.incident_window_start&&s.incident_window_end
    ?parseTime(s.incident_window_start).toLocaleTimeString("en-IN",{timeZone:caseTimezone(),hour:"2-digit",minute:"2-digit",hour12:false})+"–"+parseTime(s.incident_window_end).toLocaleTimeString("en-IN",{timeZone:caseTimezone(),hour:"2-digit",minute:"2-digit",hour12:false})+" "+zoneLabel()
    :incidentDate.toLocaleTimeString("en-IN",{timeZone:caseTimezone(),hour:"2-digit",minute:"2-digit",hour12:false})+" "+zoneLabel();
  $("#incidentTimestamp").textContent=incidentDate.toLocaleDateString("en-IN",{timeZone:caseTimezone(),day:"2-digit",month:"long",year:"numeric"})+" · "+incidentWindow;
  $("#incidentLocation").textContent=(s.incident_label||"Case location")+" · Case metadata";
  $("#railCaseName").textContent=s.case_name||s.case;
  $("#railCaseId").textContent="CASE · "+s.case;
  $("#caseLabel").textContent=(s.case||"NIGHTFALL")+" · "+(s.total||0).toLocaleString()+" ARTIFACTS LOADED";
  $("#navEvidence").textContent=(s.total||0).toLocaleString();
  $("#statTotal").textContent=(s.total||0).toLocaleString();
  $("#statPeople").textContent=(s.contacts||0).toLocaleString();
  $("#statMessages").textContent=countType("message").toLocaleString();
  $("#statCalls").textContent=countType("call").toLocaleString();
  $("#statLocations").textContent=countType("gps").toLocaleString();
  $("#statBrowser").textContent=countType("browser").toLocaleString();
  $("#statApplications").textContent=countType("application").toLocaleString();
  $("#statEvents").textContent=(s.timeline_events||0).toLocaleString();
}
async function loadDashboard(){
  $("#loadBtn").disabled=true; $("#loadBtn").innerHTML='↻ <span>Connecting…</span>';
  try {
    const [s,an,rel,corr]=await Promise.all([api("/api/stats"),api("/api/anomalies"),api("/api/relevance"),api("/api/correlations")]);
    showStats(s); clearNotice();
    const signalList=(an||[]).slice(0,3).map(a=>({title:a.label||"Anomaly signal",detail:a.detail,time:a.time,ids:a.evidence_ids||[]}));
    const relevant=(rel||[]).slice(0,2).map(r=>({title:"High relevance · "+r.type,detail:r.summary,time:r.time,ids:[r.evidence_id]}));
    const items=signalList.concat(relevant);
    $("#signals").innerHTML=items.length?items.map(x=>'<div class="signal-row"><i class="signal-dot '+(x.title.toLowerCase().includes("deletion")?"red":"")+'"></i><div><b>'+esc(x.title)+'</b><small>'+esc(fmtTime(x.time))+" · "+esc(x.detail||"")+' · '+esc((x.ids||[]).join(", "))+'</small></div><span class="signal-score">'+esc((x.ids||[])[0]||"")+"</span></div>").join("")+(corr.length?'<div class="signal-row"><i class="signal-dot"></i><div><b>'+corr.length+" correlated evidence cluster(s)</b><small>Derived from GPS fixes and timestamped artifacts</small></div></div>":""):'<div class="empty">No anomaly or relevance signals were returned for this case.</div>';
  } catch(e) {
    $("#loadBtn").innerHTML='↻ <span>Retry connection</span>';
    $("#signals").innerHTML=errorBox(e.message); notify("Could not load case statistics: "+e.message);
    return false;
  } finally { $("#loadBtn").disabled=false; }
  return true;
}
async function renderEvidence(){
  const seq=++requestSeq, q=$("#evidenceSearch").value.trim(), type=$("#typeFilter").value;
  if(q.length===1){$("#evidenceCount").textContent="Search needs at least 2 characters";$("#pageInfo").textContent="—";$("#evidenceRows").innerHTML='<tr><td colspan="6" class="empty">Enter at least two characters to search.</td></tr>';$("#prevPage").disabled=true;$("#nextPage").disabled=true;return}
  $("#evidenceRows").innerHTML='<tr><td colspan="6" class="empty">Loading evidence…</td></tr>';
  try {
    const path=q?"/api/search":"/api/evidence";
    const result=await api(path+query({q:q||undefined,type:type==="all"?"":type,source_file:$("#sourceFilter").value.trim()||undefined,device_id:$("#deviceFilter").value.trim()||undefined,start:$("#dateStart").value||undefined,end:$("#dateEnd").value||undefined,relevance_min:$("#relevanceFilter").value||undefined,offset:offset,limit:50}));
    if(seq!==requestSeq)return;
    $("#evidenceCount").textContent=result.count.toLocaleString()+" artifacts";
    $("#pageInfo").textContent=(result.count?offset+1:0)+"–"+Math.min(offset+50,result.count)+" of "+result.count;
    $("#prevPage").disabled=offset===0; $("#nextPage").disabled=offset+50>=result.count;
    $("#navEvidence").textContent=(statsData?.total??result.count).toLocaleString();
    $("#evidenceRows").innerHTML=result.results.length?result.results.map(e=>'<tr><td>'+esc(e.id)+'</td><td><span class="type-pill '+esc(e.type)+'">'+esc(e.type)+'</span></td><td>'+esc(fmtTime(e.time))+'</td><td>'+esc(e.actor||"—")+(e.target?" → "+esc(e.target):"")+'<small style="display:block;color:#83919a;margin-top:4px">'+esc(e.summary||"")+'</small></td><td>'+esc(e.source||"")+'</td><td><button class="row-open" data-id="'+esc(e.id)+'">Open ↗</button></td></tr>').join(""):'<tr><td colspan="6" class="empty">No matching evidence found. Adjust your search or type filter.</td></tr>';
    $$(".row-open").forEach(b=>b.onclick=()=>openDetail(b.dataset.id));
  } catch(e) { if(seq===requestSeq) $("#evidenceRows").innerHTML='<tr><td colspan="6">'+errorBox(e.message)+'</td></tr>'; }
}
let evidenceTimer;
$("#evidenceSearch").addEventListener("input",()=>{offset=0;clearTimeout(evidenceTimer);evidenceTimer=setTimeout(renderEvidence,220)});
$("#typeFilter").onchange=()=>{offset=0;renderEvidence()};
$("#sourceFilter").oninput=()=>{offset=0;renderEvidence()};$("#deviceFilter").oninput=()=>{offset=0;renderEvidence()};
$("#dateStart").onchange=()=>{offset=0;renderEvidence()};$("#dateEnd").onchange=()=>{offset=0;renderEvidence()};$("#relevanceFilter").onchange=()=>{offset=0;renderEvidence()};
$("#prevPage").onclick=()=>{offset=Math.max(0,offset-50);renderEvidence()};
$("#nextPage").onclick=()=>{offset+=50;renderEvidence()};
async function openDetail(id){
  const d=$("#detail"); d.classList.remove("hidden"); d.innerHTML=errorBox("Loading evidence detail…");
  try {
    const e=await api("/api/evidence/"+encodeURIComponent(id));
    d.innerHTML='<div class="detail-head"><div><div class="panel-kicker">ORIGINAL EVIDENCE · '+esc(e.case_id||"")+'</div><h2>'+esc(e.id)+' <span class="type-pill '+esc(e.type)+'">'+esc(e.type)+'</span></h2></div><button id="closeDetail" aria-label="Close">×</button></div><div class="detail-meta"><div><span>CASE ID</span><b>'+esc(e.case_id||"—")+'</b></div><div><span>SOURCE RECORD ID</span><b>'+esc(e.source_record_id||e.original_id||"—")+'</b></div><div><span>NORMALIZED TIMESTAMP · '+esc(e.timezone||"unknown timezone")+'</span><b>'+(e.time?esc(fmtTime(e.time))+" "+esc(zoneLabel()):"—")+'</b></div><div><span>ORIGINAL TIMESTAMP</span><b>'+esc(e.original_timestamp||"—")+'</b></div><div><span>PRECISION</span><b>'+esc(e.timestamp_precision||"not recorded")+'</b></div><div><span>ACTOR / TARGET</span><b>'+esc(e.actor||"—")+' → '+esc(e.target||"—")+'</b></div><div><span>SOURCE FILE</span><b>'+esc(e.source_file||e.source||"—")+'</b></div><div><span>DEVICE</span><b>'+esc(e.device_id||"—")+'</b></div><div><span>RECORD SHA-256</span><b class="hash">'+esc(e.sha256||"—")+'</b></div><div><span>SOURCE FILE SHA-256</span><b class="hash">'+esc(e.source_file_sha256||"—")+'</b></div><div><span>GPS COORDINATES</span><b>'+(e.lat==null?"—":esc(e.lat+", "+e.lon))+'</b></div></div><div class="detail-summary"><b>Normalized summary</b><p>'+esc(e.summary||"No summary")+'</p><b>Original record and metadata</b><pre style="white-space:pre-wrap;color:#93a19f;font:10px var(--mono)">'+esc(JSON.stringify(e.raw_record||e.metadata||{},null,2))+'</pre><button id="verifyHash" class="outline-btn">Verify SHA-256</button><span id="verifyResult" style="margin-left:10px;color:#86c9a8;font-size:10px"></span></div>';
    $("#closeDetail").onclick=()=>d.classList.add("hidden");
    $("#verifyHash").onclick=async()=>{const out=$("#verifyResult");out.textContent="Verifying…";try{const v=await api("/api/evidence/"+encodeURIComponent(id)+"/verify");out.textContent=v.verified?"✓ Hash verified":"Hash mismatch — integrity check failed";}catch(err){out.textContent=err.message}};
  } catch(e) { d.innerHTML=errorBox(e.message)+'<button id="closeDetail" class="outline-btn">Close</button>'; $("#closeDetail").onclick=()=>d.classList.add("hidden"); }
}
async function renderTimeline(){
  const params={limit:500,offset:timelineOffset};
  if($("#timelineFilter").value!=="all")params.types=$("#timelineFilter").value;
  if(incidentOnly)params.incident_window_min=60;
  $("#timelineRows").innerHTML='<div class="timeline-empty">Loading timeline…</div>';
  try {
    const response=await api("/api/timeline"+query(params)); timelineData=response.events||[];
    $("#timelinePageInfo").textContent=(response.count?timelineOffset+1:0)+"–"+Math.min(timelineOffset+500,response.count)+" of "+response.count;
    $("#timelinePrev").disabled=timelineOffset===0;$("#timelineNext").disabled=timelineOffset+500>=response.count;
    if(!timelineData.length){$("#timelineRows").innerHTML='<div class="timeline-empty">No matching timeline records.</div>';return}
    let last="";
    $("#timelineRows").innerHTML=timelineData.map(e=>{
      const dd=day(e.time), divider=dd!==last?(last=dd,'<div class="day-divider">'+esc(dd.toUpperCase())+"</div>"):"";
      const incident=statsData?.incident?Math.abs(parseTime(e.time)-parseTime(statsData.incident))<=1800000:false;
      const dotType=e.type==="gps"?"location":e.type==="application"?"app":e.type;
      return divider+'<div class="timeline-row"><time class="timeline-time">'+esc(parseTime(e.time).toLocaleTimeString("en-IN",{timeZone:caseTimezone(),hour:"2-digit",minute:"2-digit",hour12:false}))+'</time><span class="timeline-stem"><i class="dot '+esc(dotType)+'"></i></span><div class="timeline-copy"><b>'+esc(e.actor||"Unknown")+(e.target?" → "+esc(e.target):"")+(incident?" · INCIDENT WINDOW":"")+'</b><small>'+esc(e.summary||"")+" · "+esc(e.source||"")+'</small></div><button class="timeline-id" data-id="'+esc(e.id)+'">'+esc(e.id)+"</button></div>";
    }).join("");
    $$(".timeline-id").forEach(b=>b.onclick=()=>{go("evidence");openDetail(b.dataset.id)});
  } catch(e){$("#timelineRows").innerHTML=errorBox(e.message)}
}
$("#timelineFilter").onchange=()=>{incidentOnly=false;timelineOffset=0;$("#incidentBtn").textContent="Incident window";renderTimeline()};
$("#incidentBtn").onclick=()=>{incidentOnly=!incidentOnly;timelineOffset=0;$("#incidentBtn").textContent=incidentOnly?"Show full timeline":"Incident window";renderTimeline()};
$("#timelinePrev").onclick=()=>{timelineOffset=Math.max(0,timelineOffset-500);renderTimeline()};
$("#timelineNext").onclick=()=>{timelineOffset+=500;renderTimeline()};
async function loadGraph(){
  $("#graphCanvas").innerHTML=errorBox("Loading connections…");
  try {
    const g=await api("/api/graph");
    const edges=(g.edges||[]).map(e=>({...e,ids:e.evidence_ids||[]}));
    const nodes=(g.nodes||[]).map(n=>typeof n==="string"?n:n.id);
    graphData={nodes,edges};
    $("#graphPerson").innerHTML='<option value="">All entities</option>'+nodes.map(n=>'<option>'+esc(n)+"</option>").join("");
    renderGraph();
  }catch(e){$("#graphCanvas").innerHTML=errorBox(e.message)}
}
function renderGraph(){
  if(!graphData.nodes.length){$("#graphCanvas").innerHTML=errorBox("No message or call relationship data is available in this case.");$("#strongestConnections").innerHTML='<div class="empty">No message or call relationships in this case.</div>';$("#relationshipDetails").innerHTML='<div class="panel-kicker">RELATIONSHIP DETAILS</div><p>No communication relationships are available.</p>';return}
  const focus=$("#graphPerson").value;let nodes=graphData.nodes,edges=graphData.edges;
  if(focus){edges=edges.filter(e=>e.source===focus||e.target===focus);nodes=[...new Set([focus,...edges.flatMap(e=>[e.source,e.target])])]}
  $("#graphTotal").textContent=edges.length+" links · "+nodes.length+" entities";
  const box=$("#graphCanvas"),w=box.clientWidth||900,h=box.clientHeight||490,cx=w/2,cy=h/2,rad=Math.min(w*.38,h*.39),pos={};
  nodes.forEach((n,i)=>{const a=-Math.PI/2+2*Math.PI*i/Math.max(nodes.length,1);pos[n]=[cx+rad*Math.cos(a),cy+rad*Math.sin(a)]});
  const max=Math.max(1,...edges.map(e=>e.weight)),light=document.documentElement.dataset.theme==="light";let svg='<svg viewBox="0 0 '+w+" "+h+'" role="img" aria-label="Case communication relationship graph">';
  for(const [i,e] of edges.entries()){const [x1,y1]=pos[e.source],[x2,y2]=pos[e.target],stroke="#8b7656";svg+='<line class="g-edge" data-edge="'+i+'" x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" stroke="'+stroke+'" stroke-opacity="'+(.25+.55*e.weight/max)+'" stroke-width="'+(1+4*e.weight/max)+'"><title>'+esc(e.source)+" ↔ "+esc(e.target)+" · "+e.total_interactions+" interactions · "+esc((e.ids||[]).slice(0,8).join(", "))+"</title></line>"}
  for(const n of nodes){const [x,y]=pos[n],degree=edges.filter(e=>e.source===n||e.target===n).length,r=5+Math.min(5,degree),color="#4d9b85";svg+='<g class="g-node" data-name="'+esc(n)+'" tabindex="0"><circle cx="'+x+'" cy="'+y+'" r="'+(r+6)+'" fill="'+color+'" opacity=".1"/><circle cx="'+x+'" cy="'+y+'" r="'+r+'" fill="'+color+'" stroke="'+(light?"#ffffff":"#0e171c")+'" stroke-width="2"/><text x="'+x+'" y="'+(y+r+16)+'" fill="'+(light?"#34464d":"#c4ced2")+'" text-anchor="middle" font-size="'+Math.max(9,Math.min(12,12-nodes.length/15))+'" font-family="DM Sans,sans-serif">'+esc(n.length>20?n.slice(0,18)+"…":n)+"</text></g>"}
  box.innerHTML=svg+"</svg>";$$(".g-node").forEach(el=>el.onclick=()=>{$("#graphPerson").value=el.dataset.name;renderGraph()});
  $$(".g-edge").forEach(el=>el.onclick=()=>showRelationship(edges[Number(el.dataset.edge)]));
  const strongest=graphData.edges.slice().sort((a,b)=>b.total_interactions-a.total_interactions).slice(0,5);
  $("#strongestConnections").innerHTML=strongest.map((e,i)=>'<button class="relationship-link" data-edge="'+i+'"><b>'+esc(e.source)+" ↔ "+esc(e.target)+'</b><small>'+e.message_count+' messages · '+e.call_count+' calls</small></button>').join("")||'<div class="empty">No message or call relationships in this case.</div>';
  $$(".relationship-link").forEach((el,i)=>el.onclick=()=>showRelationship(strongest[i]));
}
function showRelationship(e){
  if(!e)return;
  const interactions=(e.interactions||[]).map(x=>'<button class="timeline-id relationship-evidence" data-id="'+esc(x.evidence_id)+'">'+esc(x.evidence_id)+' · '+esc(x.type)+' · '+esc(fmtTime(x.time))+' · '+esc(x.source_file||"")+'</button>').join("");
  $("#relationshipDetails").innerHTML='<div class="panel-kicker">SELECTED RELATIONSHIP</div><h2>'+esc(e.source)+' ↔ '+esc(e.target)+'</h2><div class="relationship-metrics"><span>'+e.message_count+' messages</span><span>'+e.call_count+' calls</span><span>'+e.total_interactions+' total interactions</span><span>'+e.call_duration_seconds+'s call duration</span></div><p>First: '+esc(fmtTime(e.first_interaction))+' · Last: '+esc(fmtTime(e.last_interaction))+' · Directions: '+esc(Object.entries(e.directions||{}).map(([k,v])=>k+' '+v).join(', '))+'</p><div class="relationship-evidence-list">'+(interactions||'<span class="empty">No source evidence IDs.</span>')+'</div>';
  $$(".relationship-evidence").forEach(b=>b.onclick=()=>{go("evidence");openDetail(b.dataset.id)});
}
$("#graphPerson").onchange=renderGraph;
async function loadMap(){
  $("#mapCanvas").innerHTML=errorBox("Loading location evidence…");
  try{
    const [loc,stats,analysis]=await Promise.all([api("/api/evidence"+query({type:"gps",limit:500})),api("/api/stats"),api("/api/map/analysis")]);
    locationData=loc.results||[];statsData=stats;geoAnalysis=analysis;
    $("#mapPerson").innerHTML='<option value="all">All people</option>'+[...new Set(locationData.map(e=>e.actor))].sort().map(n=>"<option>"+esc(n)+"</option>").join("");
    renderMap();
  }catch(e){$("#mapCanvas").innerHTML=errorBox(e.message)}
}
function renderMap(){
  let pts=locationData;const who=$("#mapPerson").value;if(who&&who!=="all")pts=pts.filter(p=>p.actor===who);
  $("#mapCount").textContent=pts.length+" fixes";
  if(!pts.length){$("#mapCanvas").innerHTML=errorBox(locationData.length?"No GPS fixes for this person.":(geoAnalysis?.message||"No geospatial evidence available."));$("#mapLegend").innerHTML="";$("#locationRows").innerHTML="";$("#geoInsights").textContent=geoAnalysis?.message||"No geospatial evidence available.";return}
  const w=800,h=470,pad=48,minLon=Math.min(...pts.map(p=>p.lon))-.008,maxLon=Math.max(...pts.map(p=>p.lon))+.008,minLat=Math.min(...pts.map(p=>p.lat))-.008,maxLat=Math.max(...pts.map(p=>p.lat))+.008,x=v=>pad+(v-minLon)/(maxLon-minLon)*(w-pad*2),y=v=>h-pad-(v-minLat)/(maxLat-minLat)*(h-pad*2),people=[...new Set(pts.map(p=>p.actor))].sort(),color=Object.fromEntries(people.map((p,i)=>[p,palette[i%palette.length]])),light=document.documentElement.dataset.theme==="light";
  let svg='<svg viewBox="0 0 '+w+" "+h+'" preserveAspectRatio="none" aria-label="GPS coordinate plot"><defs><pattern id="grid" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M 50 0 L 0 0 0 50" fill="none" stroke="'+(light?"#d5dfe2":"#243538")+'" stroke-width=".8"/></pattern></defs><rect width="'+w+'" height="'+h+'" fill="'+(light?"#f8fafb":"#111c1e")+'"/><rect width="'+w+'" height="'+h+'" fill="url(#grid)"/>';
  const incident=statsData?.incident_location;if(incident&&incident.lon>=minLon&&incident.lon<=maxLon&&incident.lat>=minLat&&incident.lat<=maxLat)svg+='<circle cx="'+x(incident.lon)+'" cy="'+y(incident.lat)+'" r="10" fill="none" stroke="#b67c2b" stroke-width="2"/><text x="'+(x(incident.lon)+11)+'" y="'+(y(incident.lat)-8)+'" fill="'+(light?"#79551d":"#e4ad5d")+'" font-size="10" font-family="DM Mono">INCIDENT · CASE LOCATION</text>';
  for(const p of people){const path=pts.filter(q=>q.actor===p).sort((a,b)=>a.time.localeCompare(b.time));if(path.length>1)svg+='<polyline points="'+path.map(q=>x(q.lon)+","+y(q.lat)).join(" ")+'" fill="none" stroke="'+color[p]+'" stroke-width="1.5" stroke-opacity=".45"/>'}
  for(const p of pts)svg+='<circle cx="'+x(p.lon)+'" cy="'+y(p.lat)+'" r="4" fill="'+color[p.actor]+'" fill-opacity=".85" stroke="'+(light?"#ffffff":"#10191b")+'" stroke-width="1.3"><title>'+esc(p.actor)+" · "+esc(fmtTime(p.time))+" · "+p.lat+", "+p.lon+" · "+esc(p.id)+"</title></circle>";
  $("#mapCanvas").innerHTML=svg+"</svg>";
  $("#mapLegend").innerHTML=people.map(p=>'<span><i class="legend-dot" style="background:'+color[p]+'"></i>'+esc(p)+"</span>").join("");
  $("#locationRows").innerHTML=pts.slice().sort((a,b)=>b.time.localeCompare(a.time)).slice(0,40).map(p=>'<div class="location-item"><time>'+esc(fmtTime(p.time))+'</time><div><b style="color:'+color[p.actor]+'">'+esc(p.actor)+'</b><small>'+p.lat.toFixed(5)+", "+p.lon.toFixed(5)+" · "+esc(p.id)+" · "+esc(p.source_file||"")+'</small></div></div>').join("");
  const geofenceCount=geoAnalysis?.geofence_events?.filter(e=>e.event==="entered_geofence").length||0,dwellCount=geoAnalysis?.dwell_periods?.length||0;
  $("#geoInsights").textContent=(geoAnalysis?.count??locationData.length)+" GPS records analyzed · "+geofenceCount+" geofence entry event(s) · "+dwellCount+" dwell period(s) · route order uses evidence timestamps.";
}
$("#mapPerson").onchange=renderMap;
function appendMessage(html){$("#chatMessages").insertAdjacentHTML("beforeend",html);$("#chatMessages").scrollTop=$("#chatMessages").scrollHeight}
async function ask(q){
  q=q.trim();if(!q)return;
  appendMessage('<div class="message-bubble user"><div class="answer-text">'+esc(q)+"</div></div>");
  $("#chatInput").value="";appendMessage('<div class="thinking">Searching case evidence…</div>');const thinking=$("#chatMessages").lastElementChild;
  try{
    const out=await api("/api/copilot",{method:"POST",body:JSON.stringify({query:q,context:{case:"NIGHTFALL"}})});
    thinking.remove();
    const citations=(out.evidence||[]).map(e=>'<button class="cite" data-id="'+esc(e.id)+'">'+esc(e.id)+"</button>").join("");
    appendMessage('<div class="assistant-msg"><div class="bot-mark">✳</div><div class="message-bubble"><div class="answer-text">'+esc(out.answer||"No answer was returned.")+'</div><div class="reasoning"><b>BACKEND EVIDENCE RETRIEVAL</b> · '+esc(out.reasoning_summary||"Retrieved from case evidence")+"</div>"+(out.assumptions||[]).map(a=>'<div class="assumption">'+esc(a)+"</div>").join("")+(citations?'<div class="evidence-cites">'+citations+"</div>":'<div class="reasoning">No supporting artifact IDs found.</div>')+"</div></div>");
    $$(".cite").forEach(b=>b.onclick=()=>{go("evidence");openDetail(b.dataset.id)});
  }catch(e){thinking.remove();appendMessage('<div class="assistant-msg"><div class="bot-mark">✳</div><div class="message-bubble">'+errorBox(e.message)+"</div></div>")}
}
$("#chatForm").onsubmit=e=>{e.preventDefault();ask($("#chatInput").value)};
$$(".suggestions button").forEach(b=>b.onclick=()=>ask(b.dataset.query));
$("#graphPerson").addEventListener("focus",()=>{if(!graphData.nodes.length)loadGraph()});
$("#mapPerson").addEventListener("focus",()=>{if(!locationData.length)loadMap()});
async function initialize(){
  startSystemStatus();
  renderEvidence();loadDashboard();refreshCaseSwitcher();
  const page=location.hash.slice(1);
  if(["dashboard","evidence","timeline","graph","map","copilot"].includes(page))go(page);
}
initialize();

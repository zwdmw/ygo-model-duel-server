"use strict";
const $ = (selector, node = document) => node.querySelector(selector);
const h = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const titles = {overview:"总览",room:"房间",models:"模型",statistics:"对局统计",decks:"卡组",replays:"录像",connections:"连接",servers:"外服配置",operations:"运维",security:"安全"};
const stateNames = {ready:"空闲",lobby:"等待开始",starting_duel:"准备对局",dueling:"对局中",warming_up:"模型预热",failed:"异常",offline:"离线"};
const labels = {recording:"录制中",complete:"已完成",incomplete:"未完成"};
const reasons = {duel_complete:"正常结束",disconnected:"连接中断",surrender:"投降",left_room:"离开房间",service_shutdown:"服务停止",timeout:"超时",error:"运行异常",recovered_after_restart:"重启恢复",interrupted:"中断"};
let csrf = "", me = null, current = "", status = null, page = 1, pollBusy = false, modalAction = null, refreshSerial = 0, filterTimer;
const replayFilter = {q:"",status:"",date:""};
let logKind = "room";
let selectedModel = "";
let selectedEvaluation = "";
let serverData = null, serverDraft = null, serverModels = [], serverDecks = [], serverDirty = false;
const statsFilter = {model:"all",days:"all",bucket:"duration"};
const outcomeNames = {win:"胜",loss:"负",draw:"平",incomplete:"未结算",unknown:"结果未知",recording:"进行中",selftest:"自测"};
const statReasons = {...reasons,engine:"引擎正常结束",human_surrender:"对手投降",truncated:"截断 / 异常终止",decision_limit:"达到决策上限",identity_mismatch:"模型身份不一致",unverified_perspective:"胜负视角未核实",invalid_reward:"结果值异常",missing_result:"缺少结果",missing_terminal_flags:"旧记录缺少结束标记",recording:"进行中",surrender_or_leave:"投降或离开未区分"};
const metricNames = {duration:"对局耗时（秒）",decisions:"模型决策次数",inference:"单局平均推理（毫秒）",startup:"开局耗时（秒）"};
const bucketNames = {...metricNames,model_deck:"模型卡组",human_deck:"对手卡组",profile:"客户端协议",reason:"结束原因"};
const decimal = value => Number.isFinite(value)?value.toLocaleString("zh-CN",{maximumFractionDigits:2}):"--";
const percent = value => Number.isFinite(value) ? (value * 100).toFixed(2) + "%" : "--";
const switchNames = {queued:"等待本局结束",loading:"正在加载",complete:"切换完成",failed:"切换失败，保留旧模型",cancelled:"已取消"};
const icon = name => `<i data-lucide="${name}"></i>`;
const icons = () => window.lucide?.createIcons();
const bytes = value => !Number.isFinite(value) ? "未取得" : value < 1048576 ? `${(value / 1024).toFixed(1)} KiB` : value < 1073741824 ? `${(value / 1048576).toFixed(1)} MiB` : `${(value / 1073741824).toFixed(1)} GiB`;
const number = value => value == null ? "--" : Number(value).toLocaleString("zh-CN");
const deckName = value => value === "BestowedDragon" ? "天杯龙" : (value || "--");
const timeText = value => value ? new Date(value).toLocaleString("zh-CN",{hour12:false}) : "--";
const badge = (text, tone="muted") => `<span class="badge ${tone}">${h(text)}</span>`;
const buttonIcon = (name,label,attrs="") => `<button class="icon-button" aria-label="${h(label)}" data-tooltip="${h(label)}" ${attrs}>${icon(name)}</button>`;
const empty = text => `<div class="empty">${icon("inbox")}${h(text)}</div>`;
function toast(text) { const el=$("#toast"); el.textContent=text; el.hidden=false; clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.hidden=true,4500); }
function showLogin() { me=null;csrf="";serverDraft=null;serverData=null;serverDirty=false;$("#view").replaceChildren();$("#login").hidden=false;$("#shell").hidden=true;current=""; }
async function api(path,body) {
  const options = {credentials:"same-origin",signal:AbortSignal.timeout(16000),headers:{}};
  if(body !== undefined) Object.assign(options,{method:"POST",body:JSON.stringify(body),headers:{"Content-Type":"application/json","X-CSRF-Token":csrf}});
  const response = await fetch("/api/"+path,options);
  const data = await response.json().catch(()=>({error:"请求失败"}));
  if(!response.ok) { if(response.status===401 && path!=="login") showLogin(); throw new Error(data.error || `请求失败 (${response.status})`); }
  if(data.csrf) csrf=data.csrf;
  return data;
}
function failure(error) { toast(error.name==="TimeoutError" ? "请求超时，请稍后重试" : error.message); }
async function activate() { me=await api("me");csrf=me.csrf;$("#account").textContent=me.username;$("#login").hidden=true;$("#shell").hidden=false;await navigate(); }
$("#login-form").addEventListener("submit",async event=>{
  event.preventDefault(); const form=event.currentTarget, button=$("button[type=submit]",form);button.disabled=true;$("#login-error").textContent="";
  try{await api("login",Object.fromEntries(new FormData(form)));form.password.value="";form.otp.value="";await activate();}
  catch(error){$("#login-error").textContent=error.message==="Unauthorized"?"账号、密码或验证码不正确":error.message;}
  finally{button.disabled=false;}
});
$("#logout").onclick=async()=>{try{await api("logout",{});showLogin();}catch(error){failure(error);}};
window.addEventListener("hashchange",()=>navigate().catch(failure));
$("#refresh").onclick=()=>refresh().catch(failure);
async function navigate() {
  const name=location.hash.slice(1);current=titles[name]?name:"overview";
  $("#page-title").textContent=titles[current];
  document.title=titles[current]+" · YGO 对战管理";
  document.querySelectorAll("nav a").forEach(a=>{a.classList.toggle("active",a.dataset.view===current);a.setAttribute("aria-current",a.dataset.view===current?"page":"false");});
  $("#view").innerHTML=empty("正在加载");
  await refresh();
}
async function fetchStatus() {
  status=await api("status");
  $("#updated").textContent="更新于 "+new Date(status.sampled_at).toLocaleTimeString("zh-CN",{hour12:false});
  const online=status.agent.online && status.agent.room?.state!=="offline";
  const marker=$("#connection-status");
  marker.textContent=online && status.bridge.online?"服务已连接":"部分服务离线";
  marker.className="badge "+(online && status.bridge.online?"good":"error");
  $("#offline").hidden=online;
}
async function refresh() {
  const serial=++refreshSerial, target=current;
  if(!me)return;
  try {
    if(["overview","room","models","statistics","connections","operations","servers"].includes(target)) await fetchStatus();
    if(serial!==refreshSerial || target!==current)return;
    if(target==="overview") renderOverview();
    else if(target==="room") renderRoom();
    else if(target==="models") {const [data,duels]=await Promise.all([api("models"),api("statistics").catch(()=>null)]);if(serial===refreshSerial)renderModels(data,duels);}
    else if(target==="statistics") {const [data,models]=await Promise.all([api("statistics?"+new URLSearchParams(statsFilter)),api("models").catch(()=>({items:[]}))]);if(serial===refreshSerial)renderStatistics(data,models);}
    else if(target==="decks") {const data=await api("decks");if(serial===refreshSerial)renderDecks(data);}
    else if(target==="replays"){const query=new URLSearchParams({...replayFilter,page});const data=await api("replays?"+query);if(serial===refreshSerial)renderReplays(data);}
    else if(target==="connections")renderConnections();
    else if(target==="servers"){
      const [data,models,decks]=await Promise.all([api("servers"),api("models").catch(()=>({items:[]})),api("decks").catch(()=>({items:[]}))]);
      if(serial===refreshSerial){serverData=data;serverModels=models.items||[];serverDecks=decks.items||[];renderServers();}
    }
    else if(target==="operations"){const [log,audit]=await Promise.all([api("logs?kind="+logKind).catch(()=>({text:"算力服务器日志暂不可用"})),api("audit")]);if(serial===refreshSerial)renderOperations(log,audit);}
    else if(target==="security"){me=await api("me");if(serial===refreshSerial)renderSecurity();}
    icons();
  } catch(error){ if(serial===refreshSerial && me){$("#view").innerHTML=empty("数据暂不可用");icons();}throw error; }
}
function details(rows){return `<dl class="details">${rows.map(([k,v,cls])=>`<div><dt>${h(k)}</dt><dd class="${cls||""}">${v}</dd></div>`).join("")}</dl>`;}
function resource(value) {
  if(!value)return empty("未取得资源信息");
  const used=value.memory_total-value.memory_available;
  return `<div class="resource-row"><div><span>CPU</span><span>${value.cpu_percent==null?"采样中":h(value.cpu_percent)+"%"}</span></div><progress max="100" value="${value.cpu_percent||0}" aria-label="CPU 占用"></progress></div>
  <div class="resource-row"><div><span>内存</span><span>${bytes(used)} / ${bytes(value.memory_total)}</span></div><progress max="${value.memory_total||1}" value="${used||0}" aria-label="内存占用"></progress></div>
  <div class="resource-row"><div><span>磁盘</span><span>剩余 ${bytes(value.disk_free)}</span></div><progress max="${value.disk_total||1}" value="${(value.disk_total-value.disk_free)||0}" aria-label="磁盘占用"></progress></div>`;
}
function renderOverview() {
  const agent=status.agent, room=agent.room||{}, online=agent.online, saved=agent.replays||{};
  const cards=[["radio","房间状态",online?(stateNames[room.state]||"--"):"离线",room.accepting===false?"新对局已暂停":"单局模式"],
    ["eye","当前观众",`${number(room.spectators)} / ${number(room.spectator_capacity)}`,room.spectating===false?"观战已关闭":"公开视角"],
    ["cpu","当前快照",room.model_label||"--",`${number(room.checkpoint_steps)} 训练步数`],
    ["film","归档录像",online?number((saved.complete||0)+(saved.incomplete||0)):"--",`已完成 ${number(saved.complete)} · 未完成 ${number(saved.incomplete)}`]];
  $("#view").innerHTML=`<section class="stats">${cards.map(([ic,title,value,detail])=>`<div class="stat"><div class="stat-label">${icon(ic)}${h(title)}</div><div class="stat-value">${h(value)}</div><div class="stat-detail">${h(detail)}</div></div>`).join("")}</section>
  <div class="split"><section class="section"><div class="section-head"><h2>当前房间</h2>${badge(online?stateNames[room.state]||room.state:"离线",online?"good":"error")}</div>${details([
    ["游戏入口",h((status.host||"47.97.218.63")+":"+(status.game_port||6008)),"mono"],
    ["默认模型卡组",h(deckName(room.default_name||room.default_model_deck))],
    ["本局模型卡组",room.occupied?h(deckName(room.model_deck)):"--"],
    ["会话",room.occupied?h(room.session||"等待开始"):"--","mono"],
    ["模型 SHA-256",h(room.checkpoint_sha256||"--"),"mono hash"],
    ["录像存储",online?h(bytes(agent.replay_bytes)):"--"],
  ])}</section><section class="section"><div class="section-head"><h2>阿里云 · 公网入口</h2>${badge(status.bridge.online?"在线":"离线",status.bridge.online?"good":"error")}</div>${resource(status.relay)}
  <div class="section-head section"><h2>算力服务器</h2>${badge(online?"在线":"离线",online?"good":"error")}</div>${online?resource(agent.resources):empty("服务器未连接")}</section></div>`;
}
function renderRoom(){
  const room=status.agent.room||{},enabled=status.agent.online&&room.management_api===1;
  $("#view").innerHTML=`<section><div class="section-head"><h2>接入与观战</h2>${badge(stateNames[room.state]||"离线",enabled?"good":"error")}</div>
  <form id="room-form" class="settings"><div class="setting-row"><label>接收新对局<input type="checkbox" name="accepting" ${room.accepting?"checked":""} ${enabled?"":"disabled"}></label></div>
  <div class="setting-row"><label>允许观战<input type="checkbox" name="spectating" ${room.spectating?"checked":""} ${enabled?"":"disabled"}></label></div>
  <div class="setting-row"><label>观众上限<input type="number" min="1" max="8" step="1" name="spectator_capacity" value="${room.spectator_capacity||8}" ${enabled?"":"disabled"}></label></div>
  <div class="setting-row"><span>每个 IP 的连接上限</span><span>${h(status.bridge.per_ip_limit||4)}</span></div>
  <div class="form-actions"><button class="primary" type="submit" ${enabled?"":"disabled"}>${icon("save")}保存设置</button></div></form></section>
  <section class="section"><div class="section-head"><h2>维护</h2></div><div class="settings">${details([["本局结束后重启",room.restart_requested?badge("已排队","error"):"未安排"],["当前对局",room.occupied?"进行中":"无"]])}
  <div class="form-actions"><button data-action="restart" ${enabled&&!room.restart_requested?"":"disabled"}>${icon("rotate-cw")}本局结束后重启</button>${room.restart_requested?`<button data-action="cancel-restart">${icon("x")}取消重启</button>`:""}</div></div></section>`;
  $("#room-form").onsubmit=async e=>{e.preventDefault();const f=e.currentTarget;await command($("button",f),()=>api("room",{action:"settings",accepting:f.accepting.checked,spectating:f.spectating.checked,spectator_capacity:Number(f.spectator_capacity.value)}),"房间设置已保存");};
}
function renderDecks(data){
  $("#view").innerHTML=`<section><div class="section-head"><h2>模型卡组</h2><button class="primary" data-action="upload">${icon("upload")}上传卡组</button></div>
  ${data.items.length?`<div class="table-wrap"><table><thead><tr><th>卡组</th><th>主卡组</th><th>额外 / 副卡组</th><th>来源</th><th></th></tr></thead><tbody>${data.items.map(d=>`<tr><td class="deck-name cell-main">${h(deckName(d.name))}</td><td>${h(d.counts.main)}</td><td>${h(d.counts.extra)} / ${h(d.counts.side)}</td><td class="nowrap">${d.builtin?"内置":"上传"}</td><td><div class="table-actions">${buttonIcon("pencil",d.builtin?"编辑副本":"编辑卡组",`data-action="edit-deck" data-id="${h(d.id)}"`)}<button data-action="default" data-id="${h(d.id)}" data-name="${h(d.name)}">设为默认</button>${buttonIcon("download","下载卡组",`data-download="decks/${h(d.id)}/download"`)}${!d.builtin?buttonIcon("trash-2","删除卡组",`data-action="delete-deck" data-id="${h(d.id)}" data-name="${h(d.name)}"`):""}</div></td></tr>`).join("")}</tbody></table></div>`:empty("暂无卡组")}</section>`;
}
function renderModels(data,duels){
  const items=data.items||[], active=data.current||{}, switching=data.switch||{};
  const busy=data.restart_requested||switching.state==="loading";
  if(!items.length){$("#view").innerHTML=empty("暂无可部署模型");return;}
  const unranked=data.ranking==="registered_unranked";
  if(!items.some(row=>row.id===selectedModel))selectedModel=items.some(row=>row.id===active.checkpoint_sha256)?active.checkpoint_sha256:items[0].id;
  const currentRow=items.find(row=>row.id===active.checkpoint_sha256), protocol=data.protocol||{};
  const actual=new Map((duels?.models||[]).map(row=>[row.id,row]));
  $("#view").innerHTML=`<section><div class="section-head"><h2>对局模型</h2>${badge(data.enabled?"服务已连接":"服务暂不可用",data.enabled?"good":"error")}</div>
  <div class="model-summary">${details([["当前模型",h(active.model_label||"--")],["当前模型测评胜率",percent(currentRow?.win_rate)],["服务器对局胜率",percent(actual.get(active.checkpoint_sha256)?.win_rate)],["训练步数",number(active.checkpoint_steps)],
    ["切换状态",h(switchNames[switching.state]||"未安排")+(switching.target?` · ${h(switching.target.model_label)}`:"")]])}</div>
  ${switching.state==="failed"?`<p class="banner error">${h(switching.error||"模型加载失败")}。新对局接入保持暂停。</p>`:""}
  <form id="model-form" class="model-picker"><label>选择模型<select id="model-choice" name="model" ${data.enabled&&!busy?"":"disabled"}>${items.map(row=>`<option value="${h(row.id)}" ${row.id===selectedModel?"selected":""}>${row.rank}. ${h(row.name)} · ${percent(row.win_rate)} · ${row.wins}/${row.games} 胜</option>`).join("")}</select></label>
  <button class="primary" type="submit" ${data.enabled&&!busy&&selectedModel!==active.checkpoint_sha256?"":"disabled"}>${icon("arrow-right-left")}切换模型</button>
  ${switching.state==="queued"&&data.restart_requested?`<button type="button" data-action="cancel-model">${icon("x")}取消切换</button>`:""}</form></section>
  <section class="section"><div class="section-head"><div><h2>${unranked?"已登记模型":"Top 10 · 测评胜率"}</h2><p>${unranked?"未提供固定测评成绩":`${h(deckName(protocol.jax_deck))} · ${number(protocol.opponents?.length)} 种对手 · 每个对手 ${number(protocol.games_per_opponent)} 局`}</p></div><span class="muted model-sort">${unranked?"登记顺序":"胜率降序，同分按训练步数降序"}</span></div>
  <div class="table-wrap"><table id="model-table"><thead><tr><th>排名</th><th>模型名称</th><th>测评胜率</th><th>测评胜 / 负 / 平</th><th>测评局数</th><th>服务器对局胜率</th><th>训练步数</th><th>测评完成时间</th><th></th></tr></thead><tbody>${items.map(row=>`<tr data-model-id="${h(row.id)}" class="${row.id===active.checkpoint_sha256?"model-current":""}"><td>${row.rank}</td><td class="cell-main">${h(row.name)} ${row.id===active.checkpoint_sha256?badge("当前","good"):""}</td><td class="win-rate">${percent(row.win_rate)}</td><td class="nowrap">${row.wins} / ${row.losses} / ${row.draws}</td><td>${row.games}</td><td><span class="win-rate">${percent(actual.get(row.id)?.win_rate)}</span><div class="cell-sub">${duels?number(actual.get(row.id)?.settled||0)+" 已结算":"未取得"}</div></td><td class="nowrap">${number(row.checkpoint_steps)}</td><td class="nowrap">${h(timeText(row.finished_at*1000))}</td><td>${buttonIcon("chart-no-axes-combined","对局统计",`data-action="model-stats" data-id="${h(row.id)}"`)}</td></tr>`).join("")}</tbody></table></div>
  <p class="notice">服务器统计含历史验收对局；胜率 = 模型胜局 / 已结算局（含平局与已确认投降）。与固定测评分别统计。</p></section>
  <section class="section"><div class="section-head"><h2>固定测评 · 对手分桶</h2><label>测评模型<select id="evaluation-model">${items.map(row=>`<option value="${h(row.id)}">${h(row.name)} · ${percent(row.win_rate)}</option>`).join("")}</select></label></div><div id="evaluation-buckets"></div></section>`;
  const renderEvaluation=()=>{
    const row=items.find(row=>row.id===$("#evaluation-model").value);
    $("#evaluation-buckets").innerHTML=`<div class="table-wrap"><table><thead><tr><th>对手</th><th>测评胜率</th><th>胜 / 负 / 平</th><th>局数</th></tr></thead><tbody>${Object.entries(row.per_opponent||{}).map(([name,result])=>`<tr><td>${h(name)}</td><td class="win-rate">${percent(result.games?result.wins/result.games:null)}</td><td>${result.wins} / ${result.losses} / ${result.draws}</td><td>${result.games}</td></tr>`).join("")}</tbody></table></div>`;
  };
  if(!items.some(row=>row.id===selectedEvaluation))selectedEvaluation=items.some(row=>row.id===active.checkpoint_sha256)?active.checkpoint_sha256:items[0].id;
  $("#evaluation-model").value=selectedEvaluation;
  $("#evaluation-model").onchange=()=>{selectedEvaluation=$("#evaluation-model").value;renderEvaluation();};renderEvaluation();
  $("#model-choice").onchange=e=>{selectedModel=e.target.value;$("#model-form button[type=submit]").disabled=!data.enabled||busy||selectedModel===active.checkpoint_sha256;};
  $("#model-form").onsubmit=e=>{
    e.preventDefault();const row=items.find(item=>item.id===selectedModel);if(!row)return;
    modal("切换对局模型",`<p>切换为 <strong>${h(row.name)}</strong>，测评胜率 <strong>${percent(row.win_rate)}</strong>（${row.wins} 胜 / ${row.games} 局）。</p><p>当前对局结束后加载，新对局暂时暂停。加载成功后恢复原接入状态；失败则保留旧模型并保持暂停。</p>`,async()=>{
      await api("models/"+row.id+"/select",{confirmation:"SWITCH_MODEL"});toast("模型切换已排队");
    },"确认切换");
  };
}
function statsDeck(value){
  const label=({"Player deck":"上传卡组","Room selection":"本地卡组",Unknown:"未记录"})[value.label]||deckName(value.label);
  return h(label)+(value.id!=="unknown"?`<div class="cell-sub mono">${h(value.id.slice(0,12))}</div>`:"");
}
function statsModel(name,id){
  return h(id==="unknown"?"模型身份未知":name)+`<div class="cell-sub mono">${id==="unknown"?"缺少快照哈希":h(id.slice(0,12))}</div>`;
}
function bucketLabel(row,dimension){
  if(metricNames[dimension])return row.id==="missing"?"未记录":row.upper==null?`≥ ${row.lower}`:`${row.lower} ≤ x < ${row.upper}`;
  if(row.id==="other")return "其他";
  if(dimension.endsWith("_deck"))return statsDeck(row);
  return h(dimension==="reason"?(statReasons[row.label]||row.label):row.label==="unknown"?"未记录":row.label);
}
function renderStatistics(data,catalog){
  const summary=data.summary,coverage=data.coverage,models=new Map((catalog.items||[]).map(row=>[row.id,{...row}]));
  for(const row of data.available_models)models.set(row.id,row);
  if(statsFilter.model!=="all"&&!models.has(statsFilter.model))models.set(statsFilter.model,{id:statsFilter.model,name:"无历史对局"});
  const modelRows=[...models.values()].sort((a,b)=>(b.checkpoint_steps||0)-(a.checkpoint_steps||0));
  const unresolved=summary.incomplete+summary.unknown;
  const cards=[["trophy","服务器对局胜率",percent(summary.win_rate),`${summary.win} 胜 / ${summary.loss} 负 / ${summary.draw} 平`],
    ["flag","已结算",number(summary.settled),`其中 ${summary.surrender_wins} 局对手投降`],
    ["circle-help","未结算 / 未知",number(unresolved),`${summary.recording} 局进行中`],
    ["activity","正常终局胜率",percent(summary.engine_win_rate),`${summary.engine_wins} 胜 / ${summary.engine_settled} 局，不含投降`]];
  $("#view").innerHTML=`<form id="stats-filters" class="toolbar stats-filters">
  <label>模型<select name="model"><option value="all">全部模型</option>${modelRows.map(row=>`<option value="${h(row.id)}" ${row.id===statsFilter.model?"selected":""}>${h(row.id==="unknown"?"模型身份未知":row.name)}${row.id==="unknown"?"":" · "+h(row.id.slice(0,8))}</option>`).join("")}</select></label>
  <label>时间范围<select name="days">${[["all","全部历史"],["1","近 24 小时"],["7","近 7 天"],["30","近 30 天"],["90","近 90 天"]].map(([v,n])=>`<option value="${v}" ${v===statsFilter.days?"selected":""}>${n}</option>`).join("")}</select></label></form>
  <section class="stats" id="duel-stats-summary">${cards.map(([ic,title,value,detail])=>`<div class="stat"><div class="stat-label">${icon(ic)}${title}</div><div class="stat-value">${value}</div><div class="stat-detail">${h(detail)}</div></div>`).join("")}</section>
  <p class="notice">模型视角；胜率 = 胜 /（胜 + 负 + 平）。明确投降计入胜局，断线、离开、截断及未知结果不计入分母。服务器记录含历史验收对局，不等同于真人天梯或固定测评。</p>
  ${coverage.invalid||coverage.omitted||coverage.missing_dates?`<p class="banner">数据覆盖：${number(coverage.invalid)} 条元数据无法读取，${number(coverage.omitted)} 条超出扫描上限，${number(coverage.missing_dates)} 条缺少日期（仅计入全部历史）。</p>`:""}
  <section class="section"><div class="section-head"><h2>运行指标</h2><span class="muted model-sort">P50 / P95 按单局指标分布统计</span></div>
  <div class="table-wrap"><table id="metric-summary"><thead><tr><th>指标</th><th>有效局数</th><th>缺失局数</th><th>平均值</th><th>P50</th><th>P95</th></tr></thead><tbody>${Object.entries(metricNames).map(([key,label])=>{const m=data.metrics[key];return `<tr><td>${h(label)}</td><td>${m.samples}</td><td>${m.missing}</td><td>${decimal(m.mean)}</td><td>${decimal(m.p50)}</td><td>${decimal(m.p95)}</td></tr>`;}).join("")}</tbody></table></div>
  <p class="notice">对局耗时含初始化；平均推理为每局总推理耗时 / 模型决策次数。缺失值单列，不按 0 计算。</p></section>
  <section class="section"><div class="section-head"><h2>指标分桶</h2><label>分桶维度<select id="stats-bucket">${Object.entries(bucketNames).map(([key,label])=>`<option value="${key}" ${statsFilter.bucket===key?"selected":""}>${h(label)}</option>`).join("")}</select></label></div>
  <div class="table-wrap"><table id="stats-buckets"><thead><tr><th>${h(bucketNames[statsFilter.bucket])}</th><th>对局数 / 占比</th><th>已结算</th><th>胜 / 负 / 平</th><th>胜率</th><th>未结算 / 未知</th><th>进行中</th></tr></thead><tbody>${data.buckets.length?data.buckets.map(row=>`<tr><td class="bucket-label">${bucketLabel(row,statsFilter.bucket)}</td><td class="bucket-share"><div>${row.total} · ${percent(summary.total?row.total/summary.total:null)}</div><progress value="${row.total}" max="${summary.total||1}" aria-label="对局占比"></progress></td><td>${row.settled}</td><td class="nowrap">${row.win} / ${row.loss} / ${row.draw}</td><td class="win-rate">${percent(row.win_rate)}</td><td>${row.incomplete+row.unknown}</td><td>${row.recording}</td></tr>`).join(""):`<tr><td colspan="7">当前筛选下没有对局</td></tr>`}</tbody></table></div></section>
  <section class="section"><div class="section-head"><h2>模型对比</h2><span class="muted model-sort">同一时间范围 · 全部历史模型</span></div>
  <div class="table-wrap"><table id="stats-models"><thead><tr><th>模型</th><th>胜率</th><th>已结算</th><th>胜 / 负 / 平</th><th>投降胜</th><th>未结算 / 未知</th><th>进行中</th><th></th></tr></thead><tbody>${data.models.length?data.models.map(row=>`<tr><td>${statsModel(row.name,row.id)}</td><td class="win-rate">${percent(row.win_rate)}</td><td>${row.settled}</td><td class="nowrap">${row.win} / ${row.loss} / ${row.draw}</td><td>${row.surrender_wins}</td><td>${row.incomplete+row.unknown}</td><td>${row.recording}</td><td>${buttonIcon("filter","筛选模型",`data-action="model-stats" data-id="${h(row.id)}"`)}</td></tr>`).join(""):`<tr><td colspan="8">当前时间范围没有对局</td></tr>`}</tbody></table></div></section>
  <section class="section"><div class="section-head"><h2>最近对局</h2><span class="muted model-sort">最近 ${data.recent.length} 局 · ${coverage.selftest_excluded} 条明确自测记录已排除</span></div>
  <div class="table-wrap"><table id="stats-recent"><thead><tr><th>会话 / 时间</th><th>模型</th><th>结果</th><th>模型卡组</th><th>对手卡组</th><th>录像状态</th><th></th></tr></thead><tbody>${data.recent.length?data.recent.map(row=>`<tr><td class="nowrap"><span class="mono">${h(row.session)}</span><div class="cell-sub">${h(timeText(row.started_at))}</div></td><td>${statsModel(row.model,row.model_id)}</td><td class="nowrap">${badge(outcomeNames[row.outcome]||row.outcome,row.outcome==="win"?"good":row.outcome==="loss"?"error":"muted")}<div class="cell-sub">${h(statReasons[row.reason]||row.reason)}</div></td><td class="deck-name">${statsDeck(row.model_deck)}</td><td class="deck-name">${statsDeck(row.human_deck)}</td><td class="nowrap">${h(labels[row.recording_status]||"未知")}</td><td>${buttonIcon("film","查看录像",`data-action="stats-replay" data-id="${h(row.session)}"`)}</td></tr>`).join(""):`<tr><td colspan="7">当前筛选下没有对局</td></tr>`}</tbody></table></div></section>`;
  $("#stats-filters").onsubmit=e=>e.preventDefault();
  $("#stats-filters").onchange=e=>{statsFilter[e.target.name]=e.target.value;refresh().catch(failure);};
  $("#stats-bucket").onchange=e=>{statsFilter.bucket=e.target.value;refresh().catch(failure);};
}
function renderReplays(data){
  $("#view").innerHTML=`<form id="filters" class="toolbar"><label>搜索<input name="q" type="search" placeholder="会话 / 卡组" value="${h(replayFilter.q)}"></label><label>状态<select name="status"><option value="">全部</option>${Object.entries(labels).map(([v,l])=>`<option value="${v}" ${replayFilter.status===v?"selected":""}>${l}</option>`).join("")}</select></label><label>日期<input name="date" type="date" value="${h(replayFilter.date)}"></label></form>
  ${data.items.length?`<div class="table-wrap"><table><thead><tr><th>会话 / 时间</th><th>模型卡组</th><th>玩家卡组</th><th>状态</th><th>文件</th><th></th></tr></thead><tbody>${data.items.map(r=>`<tr><td class="nowrap"><div class="mono">${h(r.session)}</div><div class="cell-sub">${h(timeText(r.started_at))}</div></td><td class="deck-name">${h(deckName(r.model_deck))}<div class="cell-sub">${h(r.model)}</div></td><td class="deck-name">${h(r.human_deck==="Room selection"?"本地卡组":r.human_deck)}</td><td class="nowrap">${badge(labels[r.status]||r.status,r.status==="complete"?"good":r.status==="incomplete"?"error":"muted")}<div class="cell-sub">${h(reasons[r.reason]||r.reason||"")}</div></td><td class="nowrap">${r.available?bytes(r.bytes):"--"}</td><td>${r.available?buttonIcon("tv","用本地 MDPro3 直接播放这一局（房内录像，逐帧一致）",`data-local-play-room="${h(r.session)}"`):""}${buttonIcon("download","下载录像",`data-download="replays/${h(r.session)}/download" ${r.available?"":"disabled"}`)}</td></tr>`).join("")}</tbody></table></div>`:empty("没有符合条件的录像")}
  <div class="pagination"><span>共 ${number(data.total)} 局</span><div>${buttonIcon("chevron-left","上一页",`data-action="previous" ${page<=1?"disabled":""}`)}<span>${page} / ${Math.max(1,Math.ceil(data.total/data.page_size))}</span>${buttonIcon("chevron-right","下一页",`data-action="next" ${page*data.page_size>=data.total?"disabled":""}`)}</div></div>`;
  $("#filters").onsubmit=e=>e.preventDefault();
  $("#filters").onchange=e=>{replayFilter[e.target.name]=e.target.value;page=1;refresh().catch(failure);};
  $("#filters input[name=q]").oninput=e=>{replayFilter.q=e.target.value;clearTimeout(filterTimer);filterTimer=setTimeout(()=>{page=1;refresh().then(()=>{const el=$("#filters input[name=q]");el?.focus();el?.setSelectionRange(el.value.length,el.value.length);}).catch(failure);},550);};
}
function renderConnections(){
  const bridge=status.bridge;
  $("#view").innerHTML=`<section><div class="section-head"><h2>公网连接</h2><span class="muted">${number(bridge.connections?.length||0)} 个连接</span></div>
  ${bridge.connections?.length?`<div class="table-wrap"><table><thead><tr><th>昵称</th><th>角色</th><th>IP</th><th>客户端</th><th>已连接</th><th></th></tr></thead><tbody>${bridge.connections.map(c=>`<tr><td>${h(c.name||"--")}</td><td>${c.role==="spectator"?"观众":c.role==="player"?"玩家":"连接中"}</td><td class="mono">${h(c.ip)}</td><td>${h(c.profile||"--")}</td><td class="nowrap">${Math.floor(c.seconds/60)} 分 ${c.seconds%60} 秒</td><td>${c.role==="spectator"?buttonIcon("user-x","断开观众",`data-action="kick" data-id="${h(c.id)}" data-name="${h(c.name||c.ip)}"`):""}</td></tr>`).join("")}</tbody></table></div>`:empty(bridge.online?"当前没有公网连接":"公网入口未连接")}</section>
  <section class="section"><div class="section-head"><h2>房间密码</h2></div><div class="table-wrap"><table><thead><tr><th>入口</th><th>客户端</th><th>权限</th><th></th></tr></thead><tbody>${(bridge.rooms||[]).map(r=>`<tr><td class="mono">${h(r.id)}</td><td>${r.profile==="legacy"?"MDPro3":h(r.profile)}</td><td>${r.role==="spectator"?"观战":"对战"}</td><td><div class="table-actions">${buttonIcon("key-round","修改密码",`data-action="room-password" data-id="${h(r.id)}"`)}</div></td></tr>`).join("")}</tbody></table></div></section>`;
}
function serverEdit(row){
  serverDraft={id:row?.id||"",name:row?.name||"",host:row?.host||"",port:row?.port??233,
    model_id:row?.model_id||"",deck_id:row?.deck_id??"builtin",password:"",
    deck_source:row?.deck_source||"local",auth_mode:row?.auth_mode||"none",
    account_name:row?.account_name||"",account_password:"",
    account_password_saved:!!row?.account_password_saved,forget_account:false,
    remember:!!row?.password_saved,password_saved:!!row?.password_saved,
    original_host:row?.host||"",original_port:row?.port??233,revision:serverData.revision};
  serverDirty=false;
}
function renderServers(){
  if(!serverDraft)serverEdit(serverData.items.find(row=>row.id===serverData.selected));
  const d=serverDraft,items=serverData.items;
  const modelOptions=serverModels.map(row=>({id:row.id,name:`${row.rank}. ${row.name} · ${percent(row.win_rate)}`}));
  if(d.model_id&&!modelOptions.some(row=>row.id===d.model_id))modelOptions.push({id:d.model_id,name:`已保存快照 · ${d.model_id.slice(0,12)}（当前列表不可用）`});
  const deckOptions=serverDecks.map(row=>({id:row.id,name:deckName(row.name)}));
  if(!deckOptions.some(row=>row.id==="builtin"))deckOptions.unshift({id:"builtin",name:"天杯龙"});
  if(d.deck_id&&!deckOptions.some(row=>row.id===d.deck_id))deckOptions.push({id:d.deck_id,name:`已保存卡组 · ${d.deck_id}（当前列表不可用）`});
  $("#view").innerHTML=`<section><div class="section-head"><h2>服务器预设</h2><button id="server-new">${icon("plus")}新增服务器</button></div>
    <div class="server-picker"><label>当前服务器<select id="server-choice"><option value="">新建预设</option>${items.map(row=>`<option value="${h(row.id)}" ${row.id===d.id?"selected":""}>${h(row.name)} · ${h(row.host)}:${row.port}</option>`).join("")}</select></label></div>
    <form id="server-form" class="server-form" autocomplete="off">
      <label>名称<input name="name" value="${h(d.name)}" maxlength="80" required></label>
      <div class="server-address"><label>主机<input name="host" value="${h(d.host)}" placeholder="s1.ygo233.com" maxlength="253" spellcheck="false" required></label><label>端口<input name="port" type="number" value="${h(d.port)}" min="1" max="65535" step="1" required></label></div>
      <label>模型<select name="model_id"><option value="">未指定</option>${modelOptions.map(row=>`<option value="${h(row.id)}" ${row.id===d.model_id?"selected":""}>${h(row.name)}</option>`).join("")}</select></label>
      <label>卡组来源<select name="deck_source"><option value="local" ${d.deck_source==="local"?"selected":""}>自选卡组</option><option value="server_random" ${d.deck_source==="server_random"?"selected":""}>服务器随机分配</option></select></label>
      <div class="server-deck"><label>模型卡组<select name="deck_id"><option value="">未指定</option>${deckOptions.map(row=>`<option value="${h(row.id)}" ${row.id===d.deck_id?"selected":""}>${h(row.name)}</option>`).join("")}</select></label><div class="table-actions"><button type="button" data-action="upload" data-server-deck="true">${icon("upload")}上传卡组</button>${buttonIcon("pencil","编辑所选卡组",`type="button" data-action="edit-server-deck" ${d.deck_id?"":"disabled"}`)}</div></div>
      <label>房间密码<input name="room_password" type="password" value="" maxlength="19" autocomplete="new-password" placeholder="${d.password_saved?"已保存，留空保持不变":"未保存"}" ${d.remember?"":"disabled"}></label>
      <label class="server-remember">记住房间密码<input name="remember" type="checkbox" ${d.remember?"checked":""}></label>
      <label>账号登录<select name="auth_mode"><option value="none" ${d.auth_mode==="none"?"selected":""}>不登录</option><option value="duelchronicle" ${d.auth_mode==="duelchronicle"?"selected":""}>编年史 · 随机卡组 / Match</option></select></label>
      <div class="server-account"><label>账号名<input name="account_name" value="${h(d.account_name)}" maxlength="19" autocomplete="off"></label>
      <label>账号密码<input name="account_password" type="password" maxlength="64" autocomplete="new-password" placeholder="${d.account_password_saved?"已保存，留空保持不变":"未保存"}"></label>
      <label class="server-remember">清除已保存账号密码<input name="forget_account" type="checkbox" ${d.forget_account?"checked":""}></label></div>
      <div class="form-actions"><button class="primary" type="submit">${icon("save")}保存配置</button><button type="button" id="server-reset">${icon("rotate-ccw")}撤销修改</button><button type="button" id="server-delete" class="danger" ${d.id?"":"disabled"}>${icon("trash-2")}删除</button><span id="server-dirty" class="muted">${serverDirty?"未保存":""}</span></div>
    </form></section>`;
  const form=$("#server-form");form.elements.room_password.value=d.password;
  form.elements.account_password.value=d.account_password;
  const enabled=()=>{
    const random=d.deck_source==="server_random",auth=d.auth_mode!=="none";
    form.elements.deck_id.disabled=random;
    $('[data-server-deck]',form).disabled=random;
    $('[data-action="edit-server-deck"]',form).disabled=random||!d.deck_id;
    for(const key of ["account_name","account_password","forget_account"])form.elements[key].disabled=!auth;
  };
  enabled();
  const discard=run=>{
    if(!serverDirty){run();return;}
    modal("放弃未保存修改","<p>当前配置尚未保存。</p>",async()=>{await run();},"放弃修改");
  };
  const choose=async identity=>{
    if(identity){
      serverData=await api("servers",{action:"select",revision:serverData.revision,id:identity});
      serverEdit(serverData.items.find(row=>row.id===identity));
    }else serverEdit(null);
    if(current==="servers"){renderServers();icons();}
  };
  $("#server-choice").onchange=e=>{
    const id=e.target.value;e.target.value=d.id;
    discard(()=>choose(id).catch(failure));
  };
  $("#server-new").onclick=()=>discard(()=>choose("").catch(failure));
  $("#server-reset").onclick=()=>discard(()=>{
    serverEdit(serverData.items.find(row=>row.id===d.id));renderServers();icons();
  });
  form.oninput=()=>{
    for(const key of ["name","host","port","model_id","deck_id","deck_source","auth_mode","account_name"])d[key]=form.elements[key].value;
    d.account_password=form.elements.account_password.value;
    d.forget_account=form.elements.forget_account.checked;
    d.remember=form.elements.remember.checked;
    if(!d.remember){form.elements.room_password.value="";d.password="";}
    else d.password=form.elements.room_password.value;
    form.elements.room_password.disabled=!d.remember;
    enabled();
    serverDirty=true;$("#server-dirty").textContent="未保存";
    if(typeof syncOutboundComposer==="function")syncOutboundComposer();
  };
  form.onsubmit=async e=>{
    e.preventDefault();
    const body={action:"save",revision:d.revision,id:d.id,name:d.name,host:d.host.trim(),port:Number(d.port),model_id:d.model_id,deck_id:d.deck_id,
      deck_source:d.deck_source,auth_mode:d.auth_mode,account_name:d.account_name,
      account_password_mode:d.auth_mode==="none"||d.forget_account?"forget":d.account_password?"replace":"keep",
      password_mode:!d.remember?"forget":d.password?"replace":"keep"};
    if(body.account_password_mode==="replace")body.account_password=d.account_password;
    if(body.password_mode==="replace")body.password=d.password;
    const submit=$("button[type=submit]",form);submit.disabled=true;
    try{
      serverData=await api("servers",body);
      serverEdit(serverData.items.find(row=>row.id===serverData.selected));
      if(current==="servers"){renderServers();icons();}
      toast("服务器配置已保存");
    }catch(error){failure(error);}finally{submit.disabled=false;}
  };
  $("#server-delete").onclick=()=>modal("删除服务器预设",`<p>删除 <strong>${h(d.name)}</strong> 及其保存的房间密码？</p>`,async()=>{
    serverData=await api("servers",{action:"delete",revision:d.revision,id:d.id});
    serverDraft=null;serverDirty=false;toast("服务器预设已删除");
  },"删除");
  if(typeof renderOutbound==="function")renderOutbound();
}
function renderOperations(log,audit){
  $("#view").innerHTML=`<section><div class="section-head"><h2>服务日志</h2><label><select id="log-kind" aria-label="日志类型">${[["room","房间"],["gateway","网关"],["service","服务"],["warmup","模型预热"]].map(([v,n])=>`<option value="${v}" ${logKind===v?"selected":""}>${n}</option>`).join("")}</select></label></div><pre class="code-log">${h(log.text||"暂无日志")}</pre></section>
  <section class="section"><div class="section-head"><h2>管理操作记录</h2></div>${audit.items.length?`<div class="table-wrap"><table><thead><tr><th>时间</th><th>操作</th><th>目标</th><th>结果</th></tr></thead><tbody>${audit.items.map(r=>`<tr><td class="nowrap">${h(timeText(r.at))}</td><td>${h(r.action)}</td><td>${h(r.target||"--")}</td><td>${badge(r.success?"成功":"失败",r.success?"good":"error")}</td></tr>`).join("")}</tbody></table></div>`:empty("暂无操作记录")}</section>`;
  $("#log-kind").onchange=e=>{logKind=e.target.value;refresh().catch(failure);};
}
function renderSecurity(){
  $("#view").innerHTML=`<div class="security-layout"><section><div class="section-head"><h2>管理员密码</h2></div><form id="admin-password" class="security-form">
  <label>当前密码<input name="password" type="password" autocomplete="current-password" required></label><label>新密码<input name="new_password" type="password" autocomplete="new-password" minlength="16" maxlength="128" required></label><label>确认新密码<input name="confirm" type="password" autocomplete="new-password" required></label>
  ${me.totp_enabled?`<label>动态验证码 / 恢复码<input name="otp" autocomplete="one-time-code" required></label>`:""}
  <div><button class="primary" type="submit">${icon("save")}修改密码</button></div></form></section>
  <section><div class="section-head"><h2>二次验证</h2></div><div class="security-status">${icon("shield-check")}${badge(me.totp_enabled?"已启用":"未启用",me.totp_enabled?"good":"error")}</div><button data-action="${me.totp_enabled?"totp-disable":"totp-begin"}">${icon(me.totp_enabled?"shield-off":"shield-plus")}${me.totp_enabled?"关闭二次验证":"开启二次验证"}</button></section></div>`;
  $("#admin-password").onsubmit=async e=>{e.preventDefault();const f=e.currentTarget;if(f.new_password.value!==f.confirm.value)return toast("两次新密码不一致");const body=Object.fromEntries(new FormData(f));delete body.confirm;await command($("button",f),()=>api("security",{action:"password",...body}),"管理员密码已修改");};
}
async function command(button,run,message){button.disabled=true;try{await run();await refresh();toast(message);}catch(error){failure(error);}finally{button.disabled=false;}}
function modal(title,body,action,label="确认"){
  $("#modal-title").textContent=title;$("#modal-body").innerHTML=body;$("#modal-error").textContent="";$("#modal-submit").textContent=label;$("#modal-submit").hidden=false;$("#modal-cancel").hidden=false;modalAction=action;icons();if(!$("#modal").open)$("#modal").showModal();
}
$("#modal-close").onclick=$("#modal-cancel").onclick=()=>$("#modal").close();
$("#modal").addEventListener("close",()=>{$("#modal-body").replaceChildren();modalAction=null;});
$("#modal-form").onsubmit=async event=>{
  event.preventDefault();const submit=$("#modal-submit");submit.disabled=true;$("#modal-error").textContent="";
  try { const stay=await modalAction?.(Object.fromEntries(new FormData(event.currentTarget)));if(stay!==true){$("#modal").close();await refresh();} }
  catch(error){$("#modal-error").textContent=error.message;}
  finally{submit.disabled=false;}
};
async function download(path,name){
  const response=await fetch("/api/"+path,{credentials:"same-origin",signal:AbortSignal.timeout(path.startsWith("outbound/export/")?240000:30000)});
  if(!response.ok){if(response.status===401)showLogin();const detail=await response.json().catch(()=>({}));throw new Error(detail.error||"文件暂不可下载");}
  const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");
  a.href=url;a.download=name||response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1]||"download";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
document.addEventListener("click",async event=>{
  const button=event.target.closest("button");if(!button||button.disabled)return;
  if(button.dataset.download){await command(button,()=>download(button.dataset.download),"下载已开始");return;}
  if(button.dataset.localPlay){await command(button,()=>download("outbound/"+button.dataset.localPlay+"/replay?format=yrp1",`PLAY__${button.dataset.localPlay}.yrp`),"已交给本地播放助手，稍候 MDPro3 会自动打开");return;}
  if(button.dataset.localPlayRoom){await command(button,()=>download("replays/"+button.dataset.localPlayRoom+"/download",`PLAY__room_${button.dataset.localPlayRoom}.yrp`),"已交给本地播放助手，稍候 MDPro3 会自动打开");return;}
  const action=button.dataset.action,id=button.dataset.id,name=button.dataset.name;
  if(!action)return;
  if(["upload","edit-deck","edit-server-deck"].includes(action)){
    button.disabled=true;
    try{await openDeckEditor(action==="edit-server-deck"?serverDraft?.deck_id:action==="edit-deck"?id:"",
      {selectForServer:action==="edit-server-deck"||button.dataset.serverDeck==="true"});}
    catch(error){failure(error);}finally{button.disabled=false;}
  }
  else if(action==="default"){modal("修改默认卡组",`<p>默认模型卡组改为 <strong>${h(deckName(name))}</strong>。已进入房间的玩家和当前对局不受影响。</p>`,async()=>{await api("decks/"+id+"/default",{});toast("默认卡组已更新");});}
  else if(action==="delete-deck"){modal("删除卡组",`<p>删除 <strong>${h(deckName(name))}</strong>？对局录像中的卡组副本会保留。</p>`,()=>api("decks/"+id+"/delete",{}),"删除");}
  else if(action==="restart"){modal("安排服务重启","<p>立即暂停新对局接入，等待当前对局结束后重启房间服务。重启完成后仍保持暂停，需要手动恢复接入。</p>",()=>api("room",{action:"restart",confirmation:"RESTART"}),"安排重启");}
  else if(action==="cancel-restart"){await command(button,()=>api("room",{action:"cancel_restart"}),"已取消重启，接入仍保持暂停");}
  else if(action==="cancel-model"){await command(button,()=>api("room",{action:"cancel_restart"}),"已取消切换，接入仍保持暂停");}
  else if(action==="model-stats"){statsFilter.model=id;if(current==="statistics")refresh().catch(failure);else location.hash="statistics";}
  else if(action==="stats-replay"){replayFilter.q=id;replayFilter.status="";replayFilter.date="";page=1;location.hash="replays";}
  else if(action==="room-password"){modal("修改入口密码",`<p class="mono">${h(id)}</p><label>新密码<input name="password" type="password" minlength="8" maxlength="19" pattern="[a-zA-Z0-9_-]{8,19}" autocomplete="new-password" required></label><label>确认密码<input name="confirm" type="password" autocomplete="new-password" required></label><p class="muted">8–19 位字母、数字、下划线或连字符；仅影响新连接。</p>`,async body=>{if(body.password!==body.confirm)throw new Error("两次密码不一致");await api("password",{room_id:id,password:body.password});toast("入口密码已更新");});}
  else if(action==="kick"){modal("断开观众",`<p>断开 <strong>${h(name)}</strong> 的观战连接？对局继续进行。</p>`,()=>api("kick",{id}),"断开");}
  else if(action==="previous"||action==="next"){page+=action==="next"?1:-1;refresh().catch(failure);}
  else if(action==="totp-begin"){modal("开启二次验证",`<label>当前密码<input name="password" type="password" autocomplete="current-password" required></label>`,async body=>{
    const password=body.password,setup=await api("security",{action:"totp_begin",password});
    modal("绑定验证器",`<img class="qr" src="${h(setup.qr)}" alt="验证器绑定二维码"><div class="mono">${h(setup.secret)}</div><label>动态验证码<input name="otp" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" required></label>`,async data=>{
      const result=await api("security",{action:"totp_confirm",password,otp:data.otp});
      modal("恢复码",`<p>二次验证已启用。恢复码仅显示一次，每个只能使用一次。</p><pre class="recovery">${h(result.recovery_codes.join("\n"))}</pre><button type="button" id="save-recovery">${icon("download")}下载恢复码</button>`,async()=>{},"完成");
      $("#save-recovery").onclick=()=>{const url=URL.createObjectURL(new Blob([result.recovery_codes.join("\n")],{type:"text/plain"}));const a=document.createElement("a");a.href=url;a.download="ygo-admin-recovery.txt";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
      $("#modal-cancel").hidden=true;return true;
    },"验证");return true;
  },"继续");}
  else if(action==="totp-disable"){modal("关闭二次验证",`<label>当前密码<input name="password" type="password" autocomplete="current-password" required></label><label>动态验证码 / 恢复码<input name="otp" autocomplete="one-time-code" required></label>`,data=>api("security",{action:"totp_disable",...data}),"关闭");}
});
setInterval(async()=>{
  if(!me||document.hidden||pollBusy||$("#modal").open||!["overview","connections","models","statistics"].includes(current)||document.activeElement?.matches("#model-choice,#evaluation-model,#stats-filters select,#stats-bucket"))return;
  pollBusy=true;try{await refresh();}catch(error){failure(error);}finally{pollBusy=false;}
},8000);
icons();
activate().catch(()=>showLogin());

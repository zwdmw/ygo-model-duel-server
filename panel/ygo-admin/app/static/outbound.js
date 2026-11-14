"use strict";
const outboundStates={idle:"未连接",warming:"模型预热",connecting:"连接主机",reconnecting:"正在重连",joining:"加入房间",authenticating:"登录账号",waiting:"等待对手",siding:"等待下一小局",selecting_turn:"选择先后攻",dueling:"对局中",saving:"保存结果",finished:"已结束",failed:"失败",stopped:"已停止"};
const outboundActive=new Set(["warming","connecting","reconnecting","joining","authenticating","waiting","siding","selecting_turn","dueling","saving"]);
const continuousActive=new Set(["running","waiting","stopping"]);
const continuousStates={running:"进行中",waiting:"等待下一局",stopping:"正在停止",finished:"已达目标",stopped:"已停止",failed:"异常停止"};
let outboundBusy=false,outboundRequest=false;
let outboundTarget="10";
let outboundCapacity={active:0,limit:6,available:0};
let outboundMode="rooms",outboundContinuousRunning=false;
const outboundOptions={preference:"auto",nickname:"ModelBot",version:"4962"};
const outboundCardNames=new Map();
function outboundModelOptions(selected=serverDraft?.model_id||""){
  const rows=serverModels.map(row=>({id:row.id,name:`${row.rank}. ${row.name} · ${percent(row.win_rate)}`}));
  if(selected&&!rows.some(row=>row.id===selected))rows.push({id:selected,name:`已保存快照 · ${selected.slice(0,12)}`});
  return rows.map(row=>`<option value="${h(row.id)}" ${row.id===selected?"selected":""}>${h(row.id===serverDraft?.model_id?`默认 · ${row.name}`:row.name)}</option>`).join("");
}
function outboundPasswordRow(value="",modelId=serverDraft?.model_id||""){
  return `<div class="outbound-password-row"><span class="outbound-room-index" aria-hidden="true"></span><div class="outbound-room-fields"><div class="outbound-password-input"><input class="outbound-password" type="password" maxlength="19" autocomplete="new-password" value="${h(value)}" placeholder="房间密码，留空使用预设"><button type="button" class="icon-button" data-password-visibility title="显示密码" aria-label="显示密码">${icon("eye")}</button></div><select class="outbound-model" aria-label="这个房间使用的模型">${outboundModelOptions(modelId)}</select></div><button type="button" class="icon-button" data-remove-password title="移除这个房间" aria-label="移除这个房间">${icon("x")}</button></div>`;
}
function outboundPasswordRows(){
  return [...document.querySelectorAll("#outbound-password-list .outbound-password-row")];
}
function outboundPasswordValues(){
  return outboundPasswordRows().map(row=>row.querySelector(".outbound-password").value.trim()).filter(Boolean);
}
function outboundRoomValues(){
  return outboundPasswordRows().map(row=>({
    password:row.querySelector(".outbound-password").value.trim(),
    model_id:row.querySelector(".outbound-model").value||serverDraft?.model_id||""
  }));
}
function outboundRoomLimit(){
  return Math.min(6,Math.max(0,Number(outboundCapacity.available)||0));
}
function outboundContext(){
  const form=$("#server-form");
  const selected=name=>form?.elements[name]?.selectedOptions?.[0]?.textContent?.trim()||"--";
  return {
    server:serverDraft?.name||"--",
    address:serverDraft?.host?`${serverDraft.host}:${serverDraft.port}`:"--",
    model:selected("model_id"),
    deck:serverDraft?.deck_source==="server_random"?"服务器随机分配":selected("deck_id")
  };
}
function renumberOutboundPasswords(){
  outboundPasswordRows().forEach((row,index)=>{
    row.querySelector(".outbound-room-index").textContent=String(index+1).padStart(2,"0");
    row.querySelector("[data-remove-password]").title=row.parentElement.children.length===1?"清空房间密码":"移除这个房间";
  });
}
function syncOutboundComposer(){
  const panel=$("#outbound-panel");
  if(!panel)return;
  const rows=outboundPasswordRows(),values=outboundPasswordValues();
  const blanks=rows.filter(row=>!row.querySelector(".outbound-password").value.trim()).length;
  const duplicates=new Set(values).size!==values.length;
  const requested=Math.max(1,values.length);
  const available=Math.max(0,Number(outboundCapacity.available)||0);
  const limit=Math.max(0,Number(outboundCapacity.limit)||0);
  const invalidBlank=rows.length>1&&blanks>0;
  const overCapacity=requested>available;
  const continuous=outboundMode==="continuous";
  const context=outboundContext();
  renumberOutboundPasswords();
  $("#outbound-context-server").textContent=context.server;
  $("#outbound-context-address").textContent=context.address;
  $("#outbound-context-model").textContent=context.model;
  $("#outbound-context-deck").textContent=context.deck;
  $("#outbound-queue-count").textContent=`${number(requested)} 个房间`;
  $("#outbound-settings-summary").textContent=`${$("#outbound-preference").selectedOptions[0]?.textContent||"--"} · ${outboundOptions.nickname||"--"} · ${$("#outbound-version").selectedOptions[0]?.textContent||"--"}`;
  $("#outbound-continuous-settings").hidden=!continuous;
  for(const button of panel.querySelectorAll("[data-outbound-mode]")){
    const selected=button.dataset.outboundMode===outboundMode;
    button.setAttribute("aria-pressed",selected?"true":"false");
    button.disabled=outboundBusy||(button.dataset.outboundMode==="continuous"&&outboundContinuousRunning);
  }
  $("#outbound-multi-actions").hidden=continuous;
  $("#outbound-password-add").disabled=outboundBusy||continuous||rows.length>=outboundRoomLimit();
  $("#outbound-bulk-toggle").disabled=outboundBusy||continuous||available<2;
  for(const input of panel.querySelectorAll(".outbound-password,.outbound-model"))input.disabled=outboundBusy;
  for(const button of panel.querySelectorAll("[data-remove-password],[data-password-visibility]"))button.disabled=outboundBusy;
  $("#outbound-model-reset").disabled=outboundBusy||continuous;
  for(const key of ["preference","nickname","version"])$("#outbound-"+key).disabled=outboundBusy||(key==="nickname"&&serverDraft?.auth_mode==="duelchronicle");
  $("#outbound-target").disabled=outboundBusy||!continuous||outboundContinuousRunning;
  const modelCount=new Set(outboundRoomValues().map(row=>row.model_id).filter(Boolean)).size;
  let message=`启动后占用 ${number(Math.min(limit,outboundCapacity.active+requested))} / ${number(limit)} 个槽位${modelCount>1?` · ${number(modelCount)} 个模型`:""}`;
  let invalid=false;
  if(serverDirty){message="服务器配置有未保存修改";invalid=true;}
  else if(!serverDraft?.id||!serverDraft.model_id||(serverDraft.deck_source!=="server_random"&&!serverDraft.deck_id)){message="服务器、模型或卡组尚未配置完整";invalid=true;}
  else if(invalidBlank){message="批量启动时每个房间都需要密码";invalid=true;}
  else if(duplicates){message="房间密码不能重复";invalid=true;}
  else if(continuous&&values.length>1){message="持续决斗仅支持一个房间";invalid=true;}
  else if(overCapacity){message=`当前只剩 ${number(available)} 个可用槽位`;invalid=true;}
  else if(available===0){message="当前没有可用槽位";invalid=true;}
  const summary=$("#outbound-launch-summary");
  summary.textContent=message;
  summary.className="outbound-launch-summary "+(invalid?"error":"muted");
  $("#outbound-start-label").textContent=continuous?"启动持续决斗":`启动 ${number(requested)} 个房间`;
  $("#outbound-start").disabled=outboundBusy||invalid||(continuous&&outboundContinuousRunning);
  const track=$("#outbound-capacity-track");
  if(track){
    track.style.setProperty("--slot-count",Math.max(1,limit));
    track.innerHTML=Array.from({length:limit},(_,index)=>{
      const state=index<outboundCapacity.active?"used":index<outboundCapacity.active+requested&&!invalid?"planned":"";
      return `<span class="${state}" aria-hidden="true"></span>`;
    }).join("");
  }
}
function applyOutboundPasswords(values){
  const cleaned=values.map(value=>value.trim()).filter(Boolean);
  if(!cleaned.length){toast("没有可添加的房间密码");return false;}
  if(cleaned.some(value=>value.length>19)){toast("房间密码不能超过 19 个字符");return false;}
  const unique=[...new Set(cleaned)];
  if(outboundMode==="continuous"&&unique.length>1){toast("持续决斗仅支持一个房间密码");return false;}
  const limit=outboundRoomLimit();
  if(unique.length>limit){toast(`当前最多可启动 ${number(limit)} 个房间`);return false;}
  $("#outbound-password-list").innerHTML=unique.map(value=>outboundPasswordRow(value)).join("");
  icons();
  syncOutboundComposer();
  if(unique.length<cleaned.length)toast(`已去除 ${number(cleaned.length-unique.length)} 个重复密码`);
  return true;
}
function resetOutboundPasswords(){
  const list=$("#outbound-password-list");
  if(list){
    list.innerHTML=outboundPasswordRow();
    icons();
    syncOutboundComposer();
  }
}
function outboundError(error){
  if(!error)return "";
  if(error==="stopped_by_owner")return "已手动停止，记录标为未完成";
  if(/account_login_failed/.test(error))return "账号登录失败，请检查账号名和账号密码。";
  if(/account_login_timeout/.test(error))return "账号登录超时，服务器未确认登录成功。";
  if(/room mode 1 is not a single duel/.test(error))return "算力端外服客户端版本过旧，尚未启用赛事 Match 连续对局支持。";
  if(/room mode [2-9]/.test(error))return "房间为双打或其他不支持的模式；请选择单局房间。";
  const match=error.match(/deck_rejected: (\d+)/);
  if(match){
    const value=Number(match[1]),kind=value>>>28,code=value&0x0fffffff;
    const reasons={1:"卡片数量不符合房间禁限表",2:"房间规则不允许此 OCG 卡片",3:"房间规则不允许此 TCG 卡片",4:"服务器不识别此卡片",5:"同名卡数量超限",6:"主卡组张数不合法",7:"额外卡组张数不合法",8:"副卡组张数不合法",9:"卡片尚不可用"};
    return `卡组被拒绝：${reasons[kind]||"未通过房间检查"}${code?`，${outboundCardNames.get(code)||"编号"} ${code}`:""}。`;
  }
  if(/protocol_version_mismatch/.test(error))return "客户端协议版本与目标主机不一致，请调整客户端协议。";
  if(/New-card effect scripts are pending:/.test(error))return error.replace(/^.*New-card effect scripts are pending:/,"新卡效果尚待适配，已保存未完成记录。卡号：");
  if(/Observed cards outside the model database:/.test(error))return error.replace(/^.*Observed cards outside the model database:/,"遇到当前卡池外卡片，已保存未完成记录。卡号：");
  return error;
}
function renderOutbound(){
  const section=document.createElement("section");
  section.className="section";
  section.id="outbound-panel";
  section.innerHTML=`<div class="section-head"><h2>外服对战</h2><span id="outbound-state" class="badge muted">检查中</span></div>
    <div id="outbound-runtime" class="outbound-runtime" aria-live="polite"></div>
    <form id="outbound-launch-form" class="outbound-launch-console" autocomplete="off">
      <div class="outbound-launch-head"><div><h3>启动任务</h3><p class="muted">当前预设</p></div><div class="outbound-mode-switch" role="group" aria-label="对战模式"><button type="button" data-outbound-mode="rooms" aria-pressed="true">${icon("layout-grid")}房间对战</button><button type="button" data-outbound-mode="continuous" aria-pressed="false">${icon("repeat-2")}持续决斗</button></div></div>
      <div class="outbound-context">
        <div class="outbound-context-item">${icon("server")}<span>服务器<strong id="outbound-context-server">--</strong><small id="outbound-context-address">--</small></span></div>
        <div class="outbound-context-item">${icon("cpu")}<span>模型<strong id="outbound-context-model">--</strong></span></div>
        <div class="outbound-context-item">${icon("layers")}<span>卡组<strong id="outbound-context-deck">--</strong></span></div>
      </div>
      <details class="outbound-advanced">
        <summary>连接与对战设置 <span id="outbound-settings-summary" class="muted"></span></summary>
        <div class="outbound-options">
          <label>先后攻<select id="outbound-preference"><option value="auto">自动（天杯龙后攻）</option><option value="first">优先先攻</option><option value="second">优先后攻</option><option value="random">随机</option></select></label>
          <label>昵称<input id="outbound-nickname" maxlength="19" value="${h(outboundOptions.nickname)}"></label>
          <label>客户端协议<select id="outbound-version"><option value="4962">YGOPro / YGOPro2 · 1.036.2</option><option value="4926">旧版 · 1.033.E</option></select></label>
        </div>
      </details>
      <div class="outbound-queue">
        <div class="outbound-queue-head"><div><h3>房间队列</h3><span id="outbound-queue-count" class="muted">1 个房间</span></div><div id="outbound-multi-actions" class="outbound-queue-actions"><button id="outbound-model-reset" type="button">${icon("copy-check")}全部使用默认模型</button><button id="outbound-bulk-toggle" type="button">${icon("clipboard-paste")}批量粘贴</button><button id="outbound-password-add" type="button">${icon("plus")}添加房间</button></div></div>
        <div id="outbound-password-list">${outboundPasswordRow()}</div>
        <div id="outbound-bulk" class="outbound-bulk" hidden>
          <label>批量房间密码<textarea id="outbound-bulk-values" rows="4" maxlength="240" placeholder="每行一个密码"></textarea></label>
          <div class="outbound-bulk-actions"><button id="outbound-bulk-cancel" type="button">取消</button><button id="outbound-bulk-apply" class="primary" type="button">${icon("check")}应用</button></div>
        </div>
      </div>
      <div id="outbound-continuous-settings" class="outbound-continuous-settings" hidden>
        <label for="outbound-target">${serverDraft?.auth_mode==="duelchronicle"?"胜负场数（Match，-1 不限）":"胜负局数（-1 不限）"}<input id="outbound-target" type="number" min="-1" max="2147483647" step="1" inputmode="numeric" value="${h(outboundTarget)}"></label>
      </div>
      <div class="outbound-launch-footer"><div><div id="outbound-launch-summary" class="outbound-launch-summary muted"></div><div id="outbound-capacity-track" class="outbound-capacity-track" aria-hidden="true"></div></div><button id="outbound-start" class="primary" type="submit">${icon("play")}<span id="outbound-start-label">启动 1 个房间</span></button></div>
    </form>
    <p id="outbound-continuous-status" class="outbound-progress" role="status" hidden></p>
    <p id="outbound-detail" class="muted" role="status"></p>
    <p id="outbound-pool" class="muted"></p>
    <div class="section-head section"><div><h2>活动房间</h2><p id="outbound-active-count"></p></div><button id="outbound-stop" class="danger" disabled>${icon("square")}停止全部</button></div>
    <div id="outbound-active-rooms"></div>
    <div id="outbound-statistics" class="outbound-statistics"></div>
    <div class="section-head section"><h2>外服对局记录</h2></div>
    <div class="outbound-downloads" role="group" aria-label="全部历史对局批量下载">
      <button id="outbound-export-replays" data-export="replays" title="打包全部历史外服录像 ZIP">${icon("film")}一键下载录像</button>
      <button id="outbound-export-decks" data-export="decks" title="打包全部历史对方卡组 YDK">${icon("download")}一键下载卡组</button>
      <button id="outbound-export-loss-decks" data-export="loss-decks" title="仅打包模型正式判负对局的对方卡组">${icon("download")}一键下载输局卡组</button>
    </div><div id="outbound-records"></div>`;
  $("#view").append(section);
  $("#outbound-preference").value=outboundOptions.preference;
  $("#outbound-version").value=outboundOptions.version;
  for(const key of ["preference","version","nickname"]){
    $("#outbound-"+key).oninput=e=>{outboundOptions[key]=e.target.value;syncOutboundComposer();};
  }
  for(const button of section.querySelectorAll("[data-outbound-mode]")){
    button.onclick=()=>{
      const mode=button.dataset.outboundMode;
      if(mode==="continuous"){
        const values=outboundPasswordValues();
        const modelId=outboundPasswordRows()[0]?.querySelector(".outbound-model").value;
        if(values.length>1){toast("请先将房间队列保留为一个房间");return;}
        if(outboundPasswordRows().length>1){
          $("#outbound-password-list").innerHTML=outboundPasswordRow(values[0]||"",modelId);
          icons();
        }
      }
      outboundMode=mode;
      $("#outbound-bulk").hidden=true;
      syncOutboundComposer();
    };
  }
  $("#outbound-password-add").onclick=()=>{
    const list=$("#outbound-password-list");
    const limit=outboundRoomLimit();
    if(list.children.length>=limit)return;
    list.insertAdjacentHTML("beforeend",outboundPasswordRow());
    list.lastElementChild.querySelector("input").focus();
    icons();
    syncOutboundComposer();
  };
  $("#outbound-model-reset").onclick=()=>{
    for(const select of section.querySelectorAll(".outbound-model"))select.value=serverDraft?.model_id||"";
    syncOutboundComposer();
    toast("所有房间已使用默认模型");
  };
  $("#outbound-password-list").onclick=event=>{
    const visibility=event.target.closest("[data-password-visibility]");
    if(visibility){
      const input=visibility.parentElement.querySelector("input");
      const revealing=input.type==="password";
      input.type=revealing?"text":"password";
      visibility.innerHTML=icon(revealing?"eye-off":"eye");
      visibility.title=revealing?"隐藏密码":"显示密码";
      visibility.setAttribute("aria-label",visibility.title);
      icons();
      input.focus();
      return;
    }
    const button=event.target.closest("[data-remove-password]");
    if(!button)return;
    const list=$("#outbound-password-list");
    if(list.children.length===1){
      const row=button.closest(".outbound-password-row");
      row.querySelector("input").value="";
      row.querySelector(".outbound-model").value=serverDraft?.model_id||"";
    }
    else button.closest(".outbound-password-row").remove();
    syncOutboundComposer();
  };
  $("#outbound-password-list").oninput=()=>syncOutboundComposer();
  $("#outbound-password-list").onkeydown=event=>{
    if(event.key!=="Enter"||!event.target.matches(".outbound-password"))return;
    event.preventDefault();
    const row=event.target.closest(".outbound-password-row"),next=row.nextElementSibling?.querySelector(".outbound-password");
    if(next)next.focus();
    else if(!$("#outbound-password-add").disabled)$("#outbound-password-add").click();
    else $("#outbound-start").focus();
  };
  $("#outbound-password-list").onpaste=event=>{
    if(!event.target.matches(".outbound-password"))return;
    const text=event.clipboardData?.getData("text")||"";
    if(!/[\s,，;；]/.test(text.trim()))return;
    event.preventDefault();
    applyOutboundPasswords(text.split(/[\s,，;；]+/));
  };
  $("#outbound-bulk-toggle").onclick=()=>{
    const bulk=$("#outbound-bulk");
    bulk.hidden=!bulk.hidden;
    if(!bulk.hidden)$("#outbound-bulk-values").focus();
  };
  $("#outbound-bulk-cancel").onclick=()=>{$("#outbound-bulk").hidden=true;$("#outbound-bulk-values").value="";};
  $("#outbound-bulk-apply").onclick=()=>{
    const input=$("#outbound-bulk-values");
    if(applyOutboundPasswords(input.value.split(/[\s,，;；]+/))){
      input.value="";
      $("#outbound-bulk").hidden=true;
    }
  };
  $("#outbound-target").oninput=e=>{outboundTarget=e.target.value;};
  for(const button of section.querySelectorAll("[data-export]")){
    button.onclick=async()=>{
      const buttons=[...section.querySelectorAll("[data-export]")];
      if(buttons.some(item=>item.disabled))return;
      buttons.forEach(item=>{item.disabled=true;item.setAttribute("aria-busy","true");});
      toast("正在打包");
      try{await download("outbound/export/"+button.dataset.export);toast("下载已开始");}
      catch(error){failure(error);}
      finally{buttons.forEach(item=>{item.disabled=false;item.removeAttribute("aria-busy");});}
    };
  }
  const start=async()=>{
    if(outboundBusy)return;
    if(serverDirty){toast("请先保存服务器配置");return;}
    if(!serverDraft?.id||!serverDraft.model_id||(serverDraft.deck_source!=="server_random"&&!serverDraft.deck_id)){toast("请选择已保存的服务器、模型和卡组来源");return;}
    const rows=outboundPasswordRows(),rooms=outboundRoomValues(),passwords=outboundPasswordValues();
    if(rows.length>1&&rows.some(row=>!row.querySelector(".outbound-password").value.trim())){toast("批量启动时每个房间都需要密码");return;}
    if(new Set(passwords).size!==passwords.length){toast("房间密码不能重复");return;}
    const continuous=outboundMode==="continuous";
    if(continuous&&passwords.length>1){toast("持续决斗只能使用一个房间密码");return;}
    const requested=Math.max(1,passwords.length);
    if(requested>outboundCapacity.available){toast(`当前只剩 ${number(outboundCapacity.available)} 个可用槽位`);return;}
    const body={action:continuous?"start_continuous":passwords.length>1?"start_many":"start",id:serverDraft.id,revision:serverData.revision,...outboundOptions,version:Number(outboundOptions.version)};
    if(continuous){
      const target=Number($("#outbound-target").value);
      if(!Number.isInteger(target)||(target!==-1&&(target<1||target>2147483647))){toast("胜负局数须为正整数，或 -1");return;}
      body.target=target;
    }
    if(passwords.length<=1){
      if(passwords.length===1)body.password=passwords[0];
      body.model_id=rooms[0]?.model_id||serverDraft.model_id;
    }else body.rooms=rooms;
    outboundBusy=true;syncOutboundComposer();
    try{
      const result=await api("outbound",body);
      if(section.isConnected)resetOutboundPasswords();
      toast(continuous?"持续决斗已启动":body.action==="start_many"?`已启动 ${number(result.started)} 个外服房间`:"外服对战已启动");
      await refreshOutbound();
    }catch(error){failure(error);}
    finally{outboundBusy=false;if(section.isConnected)await refreshOutbound();}
  };
  $("#outbound-launch-form").onsubmit=event=>{event.preventDefault();start();};
  $("#outbound-stop").onclick=()=>modal("停止全部外服对战","<p>停止所有活动房间及后续自动对战，并保存已接收记录？</p>",async()=>{
    await api("outbound",{action:"stop"});await refreshOutbound();
  },"全部停止");
  section.addEventListener("click",event=>{
    const button=event.target.closest("[data-stop-session]");
    if(!button||button.disabled)return;
    const session=button.dataset.stopSession,host=button.dataset.host;
    modal("停止这个房间",`<p>停止 <strong>${h(host)}</strong> 的模型连接，并保存已接收记录？其他活动房间不会受影响。</p><p class="mono">${h(session)}</p>`,async()=>{
      await api("outbound/"+session+"/stop",{});
      await refreshOutbound();
    },"停止");
  });
  syncOutboundComposer();
  refreshOutbound().catch(failure);
}
async function refreshOutbound(){
  const panel=$("#outbound-panel");
  if(!panel||outboundRequest||current!=="servers"||!me)return;
  outboundRequest=true;
  try{
    const data=await api("outbound");
    if(!panel.isConnected)return;
    const missing=[];
    for(const row of [data.current,...(data.active||[]),...data.items]){
      const match=(row.error||"").match(/deck_rejected: (\d+)/);
      if(match){
        const code=Number(match[1])&0x0fffffff;
        if(code&&!outboundCardNames.has(code))missing.push(code);
      }
    }
    if(missing.length){
      for(const code of missing)outboundCardNames.set(code,"编号");
      try{for(const card of (await api("cards?ids="+[...new Set(missing)].join(","))).items)outboundCardNames.set(card.id,card.name);}catch{}
      if(!panel.isConnected)return;
    }
    const job=data.current,batch=data.continuous||{state:"idle"};
    const jobs=Array.isArray(data.active)?data.active:(outboundActive.has(job.state)?[job]:[]);
    const capacity=data.capacity||{active:jobs.length,limit:1,available:jobs.length?0:1};
    outboundCapacity=capacity;
    const running=continuousActive.has(batch.state);
    outboundContinuousRunning=running;
    const pool=data.card_pool;
    $("#outbound-pool").textContent=(data.pool_error?`卡池异常：${data.pool_error} · 已暂停启用新卡池 · `:"")+(pool?`外服卡池 ${pool.id} · ${number(pool.card_count)} 张 · 待适配 ${number(pool.pending_script_count)} 张`:"");
    const state=$("#outbound-state");
    state.textContent=`${number(capacity.active)} / ${number(capacity.limit)} 个房间`;
    state.className="badge "+(capacity.available<=0?"error":capacity.active?"good":"muted");
    const policy=data.policy||{state:"offline"};
    const policyReady=policy.state==="ready"&&policy.backend==="gpu";
    const policyCount=Number(policy.instances)||0;
    const policyProcess=policyCount>1?`${number(policyCount)} 个模型实例`:policy.pid?number(policy.pid):"--";
    $("#outbound-runtime").innerHTML=`<div class="outbound-runtime-primary"><span>房间容量</span><strong>${number(capacity.available)} 个可用</strong></div><div class="outbound-runtime-meta"><span>${badge(policyReady?"GPU 就绪":policy.state==="warming"?"GPU 预热中":"GPU 离线",policyReady?"good":policy.state==="warming"?"muted":"error")}</span><span>共享策略进程 <strong class="mono">${policyProcess}</strong></span></div>`;
    $("#outbound-stop").disabled=!jobs.length&&!running;
    if(serverDraft?.auth_mode==="duelchronicle"){
      $("#outbound-nickname").value=serverDraft.account_name;
      outboundOptions.nickname=serverDraft.account_name;
    }
    $("#outbound-preference").options[0].textContent=serverDraft?.deck_source==="server_random"?"自动（随机先后攻）":"自动（天杯龙后攻）";
    syncOutboundComposer();
    const progress=$("#outbound-continuous-status");
    progress.hidden=batch.state==="idle";
    if(!progress.hidden){
      const wait=batch.state==="waiting"&&batch.next_at?` · ${Math.max(0,Math.ceil(batch.next_at-Date.now()/1000))} 秒后${batch.last_error?"重试":"继续"}`:"";
      progress.textContent=`持续决斗 · ${continuousStates[batch.state]||batch.state} · 胜负 ${number(batch.completed)} / ${batch.target===-1?"不限":number(batch.target)} · 胜 ${number(batch.win)} / 负 ${number(batch.loss)} · 平 ${number(batch.draw)} · 未完成 ${number(batch.incomplete)}${wait}${batch.last_error?" · "+outboundError(batch.last_error):""}`;
    }
    $("#outbound-detail").textContent=outboundError(job.error)||(job.host?`${job.host}:${job.port} · ${job.model||""} · ${deckName(job.deck)}${job.preferred_first===false?" · 优先后攻":job.preferred_first===true?" · 优先先攻":""}`:"");
    $("#outbound-active-count").textContent=jobs.length?`${number(jobs.length)} 个房间运行中`:"";
    $("#outbound-active-rooms").innerHTML=jobs.length?`<div class="outbound-room-grid">${jobs.map(r=>`<article class="outbound-room-card"><header><div><strong>${h(r.host)}:${h(r.port)}</strong><div class="cell-sub mono">${h(r.session)}</div></div><div class="outbound-room-actions">${badge(outboundStates[r.state]||r.state,r.state==="failed"?"error":"good")}${buttonIcon("square","停止这个房间",`data-stop-session="${h(r.session)}" data-host="${h(r.host)}:${h(r.port)}"`)}</div></header><dl><div><dt>模型</dt><dd>${h(r.model)}</dd></div><div><dt>卡组</dt><dd>${h(deckName(r.deck))}</dd></div><div><dt>任务</dt><dd>${r.continuous_id?"持续决斗":"房间对战"}</dd></div><div><dt>先后攻</dt><dd>${r.preferred_first===false?"优先后攻":r.preferred_first===true?"优先先攻":"自动"}</dd></div></dl></article>`).join("")}</div>`:empty("当前没有活动房间");
    const s=data.statistics;
    $("#outbound-statistics").innerHTML=`<span>胜率 <strong>${percent(s.win_rate)}</strong></span><span>胜 / 负 / 平 <strong>${s.win} / ${s.loss} / ${s.draw}</strong></span><span>未完成 <strong>${s.incomplete}</strong></span>`;
    $("#outbound-records").innerHTML=data.items.length?`<div class="table-wrap"><table><thead><tr><th>时间 / 主机</th><th>模型 / 卡组</th><th>结果</th><th>先后攻</th><th>决策 / 秒</th><th>记录</th></tr></thead><tbody>${data.items.map(r=>`<tr><td>${h(timeText(r.started_at))}<div class="cell-sub">${h(r.host)}:${h(r.port)}</div></td><td>${h(r.model)}<div class="cell-sub">${h(r.deck)}</div></td><td>${badge(r.selftest?"自测":outcomeNames[r.outcome]||outboundStates[r.state]||r.state,r.outcome==="win"?"good":r.outcome==="incomplete"?"error":"muted")}${r.error?`<div class="cell-sub outbound-error">${h(outboundError(r.error))}</div>`:""}</td><td>${r.went_first===true?"先攻":r.went_first===false?"后攻":"--"}</td><td>${number(r.decisions)} / ${decimal(r.seconds)}</td><td><div class="table-actions">${r.replay_available?buttonIcon("tv","用本地 MDPro3 直接播放这一局（自动打开客户端）",`data-local-play="${h(r.session)}"`):""}${r.trace_available?buttonIcon("play","在页面里播放这一局",`data-play="${h(r.session)}"`):""}${buttonIcon("film","下载 YRP 录像",`data-download="outbound/${h(r.session)}/replay" ${r.replay_available?"":"disabled"}`)}${buttonIcon("file-text","下载对局收发记录",`data-download="outbound/${h(r.session)}/trace" ${r.trace_available?"":"disabled"}`)}${buttonIcon("download",r.opponent_deck?.available?"下载对方卡组 YDK（主卡组和额外，无副卡组）":"对方卡组不可用：无已验证的完整录像",`data-download="outbound/${h(r.session)}/opponent-deck" ${r.opponent_deck?.available?"":"disabled"}`)}</div></td></tr>`).join("")}</tbody></table></div>`:empty("暂无外服对局");
    icons();
  }catch(error){
    if(panel.isConnected){$("#outbound-detail").textContent=error.message;$("#outbound-start").disabled=true;}
  }finally{outboundRequest=false;}
}
setInterval(()=>{if(!document.hidden)refreshOutbound();},3000);

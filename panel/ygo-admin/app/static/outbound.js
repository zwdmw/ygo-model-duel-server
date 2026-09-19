"use strict";
const outboundStates={idle:"未连接",warming:"模型预热",connecting:"连接主机",joining:"加入房间",authenticating:"登录账号",waiting:"等待对手",siding:"等待下一小局",selecting_turn:"选择先后攻",dueling:"对局中",saving:"保存结果",finished:"已结束",failed:"失败",stopped:"已停止"};
const outboundActive=new Set(["warming","connecting","joining","authenticating","waiting","siding","selecting_turn","dueling","saving"]);
const continuousActive=new Set(["running","waiting","stopping"]);
const continuousStates={running:"进行中",waiting:"等待下一局",stopping:"正在停止",finished:"已达目标",stopped:"已停止",failed:"异常停止"};
let outboundBusy=false,outboundRequest=false;
let outboundTarget="10";
const outboundOptions={preference:"auto",nickname:"ModelBot",version:"4962"};
const outboundPasswordDefault="S";
const outboundCardNames=new Map();
function outboundError(error){
  if(!error)return "";
  if(error==="stopped_by_owner")return "已手动停止，记录标为未完成";
  if(/account_login_failed/.test(error))return "账号登录失败，请检查账号名和账号密码。";
  if(/account_login_timeout/.test(error))return "账号登录超时，服务器未确认登录成功。";
  if(/room mode 1 is not a single duel/.test(error))return "当前预设不支持此 Match 房间。编年史服请选择服务器随机卡组并配置账号登录；其他服请进入单局房间。";
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
    <div class="outbound-options">
      <label>先后攻<select id="outbound-preference"><option value="auto">自动（天杯龙后攻）</option><option value="first">优先先攻</option><option value="second">优先后攻</option><option value="random">随机</option></select></label>
      <label>昵称<input id="outbound-nickname" maxlength="19" value="${h(outboundOptions.nickname)}"></label>
      <label>客户端协议<select id="outbound-version"><option value="4962">YGOPro / YGOPro2 · 1.036.2</option><option value="4926">旧版 · 1.033.E</option></select></label>
      <label>本次房间密码<input id="outbound-password" type="password" maxlength="19" value="${outboundPasswordDefault}" autocomplete="new-password" placeholder="留空使用预设"></label>
    </div>
    <div class="form-actions"><button id="outbound-start" class="primary">${icon("play")}开始对战</button><button id="outbound-stop" class="danger" disabled>${icon("square")}停止</button></div>
    <div class="outbound-continuous-controls">
      <label for="outbound-target">${serverDraft?.auth_mode==="duelchronicle"?"胜负场数（Match，-1 不限）":"胜负局数（-1 不限）"}<input id="outbound-target" type="number" min="-1" max="2147483647" step="1" inputmode="numeric" value="${h(outboundTarget)}"></label>
      <button id="outbound-continuous-start">${icon("repeat-2")}开始持续决斗</button>
    </div>
    <p id="outbound-continuous-status" class="outbound-progress" role="status" hidden></p>
    <p id="outbound-detail" class="muted" role="status"></p>
    <p id="outbound-pool" class="muted"></p>
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
    $("#outbound-"+key).oninput=e=>{outboundOptions[key]=e.target.value;};
  }
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
  const start=async continuous=>{
    if(outboundBusy)return;
    if(serverDirty){toast("请先保存服务器配置");return;}
    if(!serverDraft?.id||!serverDraft.model_id||(serverDraft.deck_source!=="server_random"&&!serverDraft.deck_id)){toast("请选择已保存的服务器、模型和卡组来源");return;}
    const body={action:continuous?"start_continuous":"start",id:serverDraft.id,revision:serverData.revision,...outboundOptions,version:Number(outboundOptions.version)};
    if(continuous){
      const target=Number($("#outbound-target").value);
      if(!Number.isInteger(target)||(target!==-1&&(target<1||target>2147483647))){toast("胜负局数须为正整数，或 -1");return;}
      body.target=target;
    }
    if($("#outbound-password").value)body.password=$("#outbound-password").value;
    outboundBusy=true;$("#outbound-start").disabled=true;$("#outbound-continuous-start").disabled=true;
    try{
      await api("outbound",body);
      if(section.isConnected)$("#outbound-password").value=outboundPasswordDefault;
      toast(continuous?"持续决斗已启动":"外服对战已启动");
      await refreshOutbound();
    }catch(error){failure(error);}
    finally{outboundBusy=false;if(section.isConnected)await refreshOutbound();}
  };
  $("#outbound-start").onclick=()=>start(false);
  $("#outbound-continuous-start").onclick=()=>start(true);
  $("#outbound-stop").onclick=()=>modal("停止外服对战","<p>停止当前对局及后续自动对战，并保存已接收记录？</p>",async()=>{
    await api("outbound",{action:"stop"});await refreshOutbound();
  },"停止");
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
    for(const row of [data.current,...data.items]){
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
    const active=outboundActive.has(job.state),running=continuousActive.has(batch.state),busy=active||running;
    const pool=data.card_pool;
    $("#outbound-pool").textContent=(data.pool_error?`卡池异常：${data.pool_error} · 已暂停启用新卡池 · `:"")+(pool?`外服卡池 ${pool.id} · ${number(pool.card_count)} 张 · 待适配 ${number(pool.pending_script_count)} 张`:"");
    const state=$("#outbound-state");
    state.textContent=outboundStates[job.state]||job.state;
    state.className="badge "+(job.state==="failed"?"error":active?"good":"muted");
    $("#outbound-start").disabled=busy||outboundBusy;
    $("#outbound-continuous-start").disabled=busy||outboundBusy;
    $("#outbound-target").disabled=busy||outboundBusy;
    $("#outbound-stop").disabled=!busy;
    for(const key of ["preference","nickname","version","password"])$("#outbound-"+key).disabled=busy;
    if(serverDraft?.auth_mode==="duelchronicle"){
      $("#outbound-nickname").value=serverDraft.account_name;
      $("#outbound-nickname").disabled=true;
    }
    $("#outbound-preference").options[0].textContent=serverDraft?.deck_source==="server_random"?"自动（随机先后攻）":"自动（天杯龙后攻）";
    const progress=$("#outbound-continuous-status");
    progress.hidden=batch.state==="idle";
    if(!progress.hidden){
      const wait=batch.state==="waiting"&&batch.next_at?` · ${Math.max(0,Math.ceil(batch.next_at-Date.now()/1000))} 秒后${batch.last_error?"重试":"继续"}`:"";
      progress.textContent=`持续决斗 · ${continuousStates[batch.state]||batch.state} · 胜负 ${number(batch.completed)} / ${batch.target===-1?"不限":number(batch.target)} · 胜 ${number(batch.win)} / 负 ${number(batch.loss)} · 平 ${number(batch.draw)} · 未完成 ${number(batch.incomplete)}${wait}${batch.last_error?" · "+outboundError(batch.last_error):""}`;
    }
    $("#outbound-detail").textContent=outboundError(job.error)||(job.host?`${job.host}:${job.port} · ${job.model||""} · ${deckName(job.deck)}${job.preferred_first===false?" · 优先后攻":job.preferred_first===true?" · 优先先攻":""}`:"");
    const s=data.statistics;
    $("#outbound-statistics").innerHTML=`<span>胜率 <strong>${percent(s.win_rate)}</strong></span><span>胜 / 负 / 平 <strong>${s.win} / ${s.loss} / ${s.draw}</strong></span><span>未完成 <strong>${s.incomplete}</strong></span>`;
    $("#outbound-records").innerHTML=data.items.length?`<div class="table-wrap"><table><thead><tr><th>时间 / 主机</th><th>模型 / 卡组</th><th>结果</th><th>先后攻</th><th>决策 / 秒</th><th>记录</th></tr></thead><tbody>${data.items.map(r=>`<tr><td>${h(timeText(r.started_at))}<div class="cell-sub">${h(r.host)}:${h(r.port)}</div></td><td>${h(r.model)}<div class="cell-sub">${h(r.deck)}</div></td><td>${badge(r.selftest?"自测":outcomeNames[r.outcome]||outboundStates[r.state]||r.state,r.outcome==="win"?"good":r.outcome==="incomplete"?"error":"muted")}${r.error?`<div class="cell-sub outbound-error">${h(outboundError(r.error))}</div>`:""}</td><td>${r.went_first===true?"先攻":r.went_first===false?"后攻":"--"}</td><td>${number(r.decisions)} / ${decimal(r.seconds)}</td><td><div class="table-actions">${r.replay_available?buttonIcon("tv","用本地 MDPro3 直接播放这一局（自动打开客户端）",`data-local-play="${h(r.session)}"`):""}${r.trace_available?buttonIcon("play","在页面里播放这一局",`data-play="${h(r.session)}"`):""}${buttonIcon("film","下载 YRP 录像",`data-download="outbound/${h(r.session)}/replay" ${r.replay_available?"":"disabled"}`)}${buttonIcon("file-text","下载对局收发记录",`data-download="outbound/${h(r.session)}/trace" ${r.trace_available?"":"disabled"}`)}${buttonIcon("download",r.opponent_deck?.available?"下载对方卡组 YDK（主卡组和额外，无副卡组）":"对方卡组不可用：无已验证的完整录像",`data-download="outbound/${h(r.session)}/opponent-deck" ${r.opponent_deck?.available?"":"disabled"}`)}</div></td></tr>`).join("")}</tbody></table></div>`:empty("暂无外服对局");
    icons();
  }catch(error){
    if(panel.isConnected){$("#outbound-detail").textContent=error.message;$("#outbound-start").disabled=true;$("#outbound-continuous-start").disabled=true;}
  }finally{outboundRequest=false;}
}
setInterval(()=>{if(!document.hidden)refreshOutbound();},3000);

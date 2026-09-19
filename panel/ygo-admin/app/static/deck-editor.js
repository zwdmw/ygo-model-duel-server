"use strict";
const deckSections = {main:"主卡组",extra:"额外卡组",side:"副卡组"};
function cardPoolLabel(card){
  if(card?.pending_script)return " · 新卡效果待适配";
  if(card?.supported===false)return " · 当前卡池不支持";
  return card?.hosted_supported===false&&card?.external_supported?" · 仅外服卡池":"";
}
const cosmeticSections = new Set(["#pickup","#case","#protector","#field","#grave","#stand","#mate"]);
function parseEditorYdk(text){
  if(new TextEncoder().encode(text).length>65536)throw new Error("YDK 文件不能超过 64 KiB");
  const cards={main:[],extra:[],side:[]};let section="main",total=0;
  text.replace(/^\uFEFF/,"").split(/\r?\n/).forEach((raw,index)=>{
    const line=raw.trim();
    if(["#main","#extra","!side"].includes(line))section=line.slice(1);
    else if(cosmeticSections.has(line))section=null;
    else if(!line||line.startsWith("#")||section===null)return;
    else if(/^[0-9]+$/.test(line)&&Number(line)>0&&Number(line)<=4294967295){
      cards[section].push(Number(line));if(++total>500)throw new Error("卡组超过 500 张，无法导入");
    }else throw new Error(`第 ${index+1} 行不是有效卡号`);
  });
  return cards;
}
function editorYdk(cards){
  return ["main","extra","side"].map(key=>(key==="side"?"!":"#")+key+"\n"+cards[key].map(String).join("\n")+(cards[key].length?"\n":"")).join("");
}
async function openDeckEditor(identity="",options={}){
  const source=identity?await api("decks/"+identity):{id:"",name:"",ydk:"#main\n#extra\n!side\n",builtin:false};
  const editor={source,cards:parseEditorYdk(source.ydk),section:"main",mode:"cards",copy:!!source.builtin,
    names:new Map(),closed:false,searchSerial:0,metadataSerial:0,timer:null};
  const counts=()=>Object.entries(deckSections).map(([key,label])=>
    `${label} ${editor.cards[key].length}`).join(" · ");
  const error=message=>{$("#modal-error").textContent=message;};
  const readRaw=()=>{
    if(editor.mode==="ydk")editor.cards=parseEditorYdk($("#editor-ydk").value);
    return editor.cards;
  };
  modal(source.id?"编辑卡组":"上传卡组",`
    <div class="deck-editor-head">
      <label>卡组名称<input name="name" id="editor-name" value="${h(source.builtin?deckName(source.name)+" 副本":source.name)}" maxlength="120" required></label>
      <label>YDK 文件<input id="editor-file" name="file" type="file" accept=".ydk"></label>
    </div>
    <div class="deck-editor-controls">
      <div class="editor-modes" role="group" aria-label="编辑模式"><button type="button" data-editor-mode="cards" aria-pressed="true">${icon("list")}卡片</button><button type="button" data-editor-mode="ydk" aria-pressed="false">${icon("file-code")}YDK</button></div>
      ${source.id?`<label class="editor-copy-label">另存为新卡组<input id="editor-copy" type="checkbox" ${editor.copy?"checked":""} ${source.builtin?"disabled":""}></label>`:""}
      ${buttonIcon("download","导出当前 YDK",'id="editor-export" type="button"')}
    </div>
    <div id="editor-counts" class="muted"></div>
    <div id="editor-card-mode">
      <div class="editor-tabs" role="tablist" aria-label="卡组分区">${Object.entries(deckSections).map(([key,label])=>`<button type="button" role="tab" data-editor-section="${key}" aria-selected="${key==="main"}">${label}<span data-count="${key}">0</span></button>`).join("")}</div>
      <div class="deck-editor-grid">
        <section class="editor-list"><div id="editor-cards" class="table-wrap"></div></section>
        <section class="editor-search"><label>搜索卡名 / 卡号<input id="editor-search" type="search" maxlength="80" autocomplete="off"></label><div id="editor-search-count" class="muted"></div><div id="editor-search-results"></div></section>
      </div>
    </div>
    <label id="editor-raw-mode" hidden>YDK 内容<textarea id="editor-ydk" name="ydk" spellcheck="false" rows="14"></textarea></label>`,
    async body=>{
      const cards=readRaw(),ydk=editorYdk(cards);
      if(cards.main.length<40||cards.main.length>60||cards.extra.length>15||cards.side.length>15)
        throw new Error("主卡组须为 40–60 张，额外卡组和副卡组各不超过 15 张");
      const updating=source.id&&!editor.copy;
      const payload={name:body.name,ydk};
      if(updating)payload.revision=source.revision;
      const saved=await api(updating?"decks/"+source.id+"/edit":"decks",payload);
      if(options.selectForServer&&serverDraft){
        serverDraft.deck_id=saved.id;serverDirty=true;
      }
      toast(updating?"卡组已保存；房间默认卡组保持不变":"卡组已上传并选入卡组库");
    },editor.copy?"保存副本":"保存卡组");
  const dialog=$("#modal");dialog.classList.add("deck-editor-modal");
  dialog.addEventListener("close",()=>{
    editor.closed=true;clearTimeout(editor.timer);dialog.classList.remove("deck-editor-modal");
  },{once:true});
  $("#editor-ydk").value=editorYdk(editor.cards);
  const renderCounts=()=>{
    $("#editor-counts").textContent=counts();
    document.querySelectorAll("[data-count]").forEach(el=>el.textContent=editor.cards[el.dataset.count].length);
  };
  const renderCards=()=>{
    renderCounts();
    const groups=new Map();
    for(const id of editor.cards[editor.section])groups.set(id,(groups.get(id)||0)+1);
    $("#editor-cards").innerHTML=groups.size?`<table><thead><tr><th>卡片</th><th>数量</th><th>分区</th><th></th></tr></thead><tbody>${[...groups].map(([id,count])=>{
      const card=editor.names.get(id),validSections=card?.supported?[card.extra?"extra":"main","side"]:Object.keys(deckSections);
      return `<tr data-card="${id}"><td class="editor-card-name"><div>${h(card?.name||id)}</div><div class="mono muted">${id}${cardPoolLabel(card)}</div></td><td><div class="editor-stepper">${buttonIcon("minus","减少一张",`type="button" data-card-change="${id}" data-delta="-1"`)}<input type="number" min="1" max="60" step="1" value="${count}" aria-label="${id} 数量" data-card-count="${id}">${buttonIcon("plus","增加一张",`type="button" data-card-change="${id}" data-delta="1"`)}</div></td><td><select aria-label="${id} 分区" data-card-move="${id}">${[...new Set([editor.section,...validSections])].map(key=>`<option value="${key}" ${editor.section===key?"selected":""}>${deckSections[key]}</option>`).join("")}</select></td><td>${buttonIcon("trash-2","移除卡片",`type="button" data-card-remove="${id}"`)}</td></tr>`;
    }).join("")}</tbody></table>`:empty("该分区没有卡片");
    icons();
  };
  const loadNames=async()=>{
    const ids=[...new Set(Object.values(editor.cards).flat())],serial=++editor.metadataSerial;
    if(!ids.length){renderCards();return;}
    try{
      const data=await api("cards?"+new URLSearchParams({ids:ids.join(",")}));
      if(editor.closed||serial!==editor.metadataSerial)return;
      data.items.forEach(row=>editor.names.set(row.id,row));renderCards();
    }catch(exc){if(!editor.closed&&serial===editor.metadataSerial)error("卡片名称暂不可用："+exc.message);}
  };
  const search=async()=>{
    const q=$("#editor-search").value.trim(),serial=++editor.searchSerial;
    $("#editor-search-count").textContent="";
    if(!q){$("#editor-search-results").replaceChildren();return;}
    $("#editor-search-results").innerHTML=empty("正在搜索");
    try{
      const data=await api("cards?"+new URLSearchParams({q,section:editor.section}));
      if(editor.closed||serial!==editor.searchSerial)return;
      data.items.forEach(row=>editor.names.set(row.id,row));
      $("#editor-search-count").textContent=`${data.total} 张匹配${data.total>data.items.length?` · 显示前 ${data.items.length} 张`:""}`;
      $("#editor-search-results").innerHTML=data.items.length?data.items.map(row=>
        `<div class="editor-search-row"><div><span>${h(row.name)}</span><div class="mono muted">${row.id}${cardPoolLabel(row)}</div></div>${buttonIcon("plus","加入"+deckSections[editor.section],`type="button" data-card-add="${row.id}" ${row.supported===false?"disabled":""}`)}</div>`).join(""):empty("未找到匹配卡片");
      icons();
    }catch(exc){if(!editor.closed&&serial===editor.searchSerial)$("#editor-search-results").innerHTML=empty(exc.message);}
  };
  const setCount=(id,count)=>{
    if(!Number.isInteger(count)||count<0||count>60)throw new Error("卡片数量须为 0–60 的整数");
    const cards=editor.cards[editor.section],previous=cards.filter(value=>value===id).length;
    if(count<previous){
      let removed=previous-count;
      for(let index=cards.length-1;index>=0&&removed;index--)if(cards[index]===id){cards.splice(index,1);removed--;}
    }else for(let n=previous;n<count;n++)cards.push(id);
  };
  $("#editor-search").oninput=()=>{clearTimeout(editor.timer);++editor.searchSerial;editor.timer=setTimeout(search,250);};
  $("#editor-file").onchange=async e=>{
    const file=e.target.files[0];if(!file)return;
    $("#modal-submit").disabled=true;
    try{
      if(file.size>65536)throw new Error("YDK 文件不能超过 64 KiB");
      const parsed=parseEditorYdk(await file.text());
      if(editor.closed)return;
      editor.cards=parsed;$("#editor-ydk").value=editorYdk(parsed);
      if(!$("#editor-name").value.trim())$("#editor-name").value=file.name.replace(/\.ydk$/i,"");
      error("");renderCards();loadNames();
    }catch(exc){if(!editor.closed)error(exc.message);}
    finally{if(!editor.closed){e.target.value="";$("#modal-submit").disabled=false;}}
  };
  $("#editor-copy")?.addEventListener("change",e=>{
    editor.copy=e.target.checked;$("#modal-submit").textContent=editor.copy?"保存副本":"保存卡组";
  });
  $("#editor-ydk").oninput=()=>{
    try{editor.cards=parseEditorYdk($("#editor-ydk").value);renderCounts();error("");}
    catch(exc){error(exc.message);}
  };
  $("#modal-body").onclick=event=>{
    const button=event.target.closest("button");if(!button)return;
    try{
      if(button.dataset.editorMode){
        readRaw();editor.mode=button.dataset.editorMode;$("#editor-ydk").value=editorYdk(editor.cards);
        $("#editor-card-mode").hidden=editor.mode!=="cards";$("#editor-raw-mode").hidden=editor.mode!=="ydk";
        document.querySelectorAll("[data-editor-mode]").forEach(el=>el.setAttribute("aria-pressed",el.dataset.editorMode===editor.mode));
        if(editor.mode==="cards"){renderCards();loadNames();}
      }else if(button.dataset.editorSection){
        editor.section=button.dataset.editorSection;
        document.querySelectorAll("[data-editor-section]").forEach(el=>el.setAttribute("aria-selected",el.dataset.editorSection===editor.section));
        renderCards();search();
      }else if(button.dataset.cardChange){
        const id=Number(button.dataset.cardChange),count=editor.cards[editor.section].filter(value=>value===id).length;
        setCount(id,count+Number(button.dataset.delta));renderCards();
      }else if(button.dataset.cardRemove){setCount(Number(button.dataset.cardRemove),0);renderCards();}
      else if(button.dataset.cardAdd){
        const id=Number(button.dataset.cardAdd);
        setCount(id,editor.cards[editor.section].filter(value=>value===id).length+1);renderCards();
      }else if(button.id==="editor-export"){
        const url=URL.createObjectURL(new Blob([editorYdk(readRaw())],{type:"text/plain;charset=utf-8"})),a=document.createElement("a");
        a.href=url;a.download=($("#editor-name").value||"deck").replace(/[\\/:*?"<>|]/g,"_")+".ydk";
        a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      }
      error("");
    }catch(exc){error(exc.message);}
  };
  $("#modal-body").onchange=e=>{
    try{
      if(e.target.dataset.cardCount){
        const count=Number(e.target.value);setCount(Number(e.target.dataset.cardCount),count);
        if(count===0)renderCards();else renderCounts();
      }
      else if(e.target.dataset.cardMove){
        const id=Number(e.target.dataset.cardMove),count=editor.cards[editor.section].filter(value=>value===id).length;
        setCount(id,0);editor.cards[e.target.value].push(...Array(count).fill(id));renderCards();
      }
    }catch(exc){error(exc.message);}
  };
  dialog.addEventListener("close",()=>{$("#modal-body").onclick=null;$("#modal-body").onchange=null;},{once:true});
  renderCards();loadNames();
}

"use strict";
/* In-page playback of a recorded external duel.
 *
 * The frames come from the agent, which replays the session's own network
 * journal through the same shadow board the external client used, so only what
 * that client could observe is shown. Card names are resolved here through the
 * existing card endpoint; the frames carry codes.
 */
const replayView = {identity:"", frames:[], names:new Map(), index:0, timer:null, speed:1,
                    playing:false, total:0};

const PHASE_TEXT = {0:"准备", 1:"抽卡", 2:"准备", 4:"主要 1", 8:"战斗", 16:"主要 2", 32:"结束"};
const EVENT_TONE = {decision:"good", win:"error", lp:"error", summon:"good", chain:"good"};

function replayCardName(code) {
  if (!code) return "";
  return replayView.names.get(code) || replayView.names.get(Number(code)) || `#${code}`;
}

function replaySlot(card) {
  if (!card || (!card.c && !card.d)) return `<div class="rv-slot empty"></div>`;
  if (!card.c) return `<div class="rv-slot facedown" title="盖放的卡">盖</div>`;
  const name = replayCardName(card.c);
  const atk = card.a ? `<span class="rv-atk">${number(card.a)}</span>` : "";
  return `<div class="rv-slot${card.d ? " facedown" : ""}" title="${h(name)}">${atk}` +
         `<span class="rv-name">${h(name.length > 7 ? name.slice(0, 7) + "…" : name)}</span></div>`;
}

function replayPlayer(seat, frame) {
  const monsters = (frame.mon[seat] || []).slice(0, 7);
  const spells = (frame.st[seat] || []).slice(0, 8);
  const counts = frame.cnt[seat] || {};
  return `<section class="rv-player">
    <div class="rv-row">${monsters.map(replaySlot).join("")}</div>
    <div class="rv-row">${spells.map(replaySlot).join("")}</div>
    <div class="rv-counts">
      <span>手牌 <strong>${number(counts.hand)}</strong></span>
      <span>墓地 <strong>${number(counts.grave)}</strong></span>
      <span>除外 <strong>${number(counts.banished)}</strong></span>
      <span>卡组 <strong>${number(counts.deck)}</strong></span>
      <span>额外 <strong>${number(counts.extra)}</strong></span>
    </div>
  </section>`;
}

function replayRender() {
  const frame = replayView.frames[replayView.index];
  if (!frame) return;
  const seat = frame.seat_model;
  const ours = seat === 0 ? "1" : "0";
  $("#rv-meta").textContent =
    `第 ${replayView.index + 1} / ${replayView.frames.length} 帧 · 回合 ${frame.t} · ` +
    `${PHASE_TEXT[frame.ph] || "阶段"} · ${frame.ev === "decision" ? "决策" : frame.txt}`;
  $("#rv-board").innerHTML =
    `<div class="rv-side"><div class="rv-side-head">对手（seat ${ours}）</div>${replayPlayer(ours, frame)}</div>` +
    `<div class="rv-side"><div class="rv-side-head">模型（seat ${seat}）</div>${replayPlayer(String(seat), frame)}</div>`;
  const lp = frame.lp || [0, 0];
  $("#rv-score").innerHTML =
    `<span class="rv-lp">对手 <strong>${number(lp[Number(ours)])}</strong></span>` +
    `<span class="rv-vs">LP</span>` +
    `<span class="rv-lp">模型 <strong>${number(lp[seat])}</strong></span>`;
  const act = frame.act;
  $("#rv-action").innerHTML = act
    ? `${badge("模型决策", "good")} <strong>${h(act.label || "选择")}</strong>` +
      (act.code ? ` ${h(replayCardName(act.code))}` : "") +
      (act.index != null ? ` <span class="muted">选项 ${act.index + 1}/${act.options}</span>` : "") +
      (act.actor != null ? ` <span class="muted">seat ${act.actor}</span>` : "")
    : `${badge(frame.txt || "事件", EVENT_TONE[frame.ev] || "muted")}`;
  $("#rv-range").value = String(replayView.index);
  const log = $("#rv-log");
  log.innerHTML = replayView.frames.map((row, position) => {
    const label = row.act
      ? `${row.act.label || "选择"}${row.act.code ? " " + h(replayCardName(row.act.code)) : ""}`
      : h(row.txt || row.ev);
    return `<button class="rv-log-row${position === replayView.index ? " current" : ""}" ` +
           `data-jump="${position}"><span class="rv-log-turn">T${row.t}</span>` +
           `<span class="rv-log-text">${label}</span></button>`;
  }).join("");
  const current = $(`.rv-log-row.current`);
  if (current) current.scrollIntoView({block: "nearest"});
}

function replayShow(index) {
  const last = replayView.frames.length - 1;
  replayView.index = Math.max(0, Math.min(last, index));
  replayRender();
}

function replayStep(delta) {
  replayShow(replayView.index + delta);
}

function replayDecisions(direction) {
  const frames = replayView.frames;
  for (let position = replayView.index + direction; position >= 0 && position < frames.length;
       position += direction) {
    if (frames[position].ev === "decision") { replayShow(position); return; }
  }
}

function replayTick() {
  if (!replayView.playing) return;
  if (replayView.index >= replayView.frames.length - 1) { replayToggle(false); return; }
  replayStep(1);
  replayView.timer = setTimeout(replayTick, 700 / replayView.speed);
}

function replayToggle(playing) {
  replayView.playing = playing === undefined ? !replayView.playing : playing;
  clearTimeout(replayView.timer);
  const button = $("#rv-play");
  if (button) button.innerHTML = icon(replayView.playing ? "pause" : "play");
  if (replayView.playing) replayTick();
  icons();
}

function replayClose() {
  replayToggle(false);
  $("#rv-overlay")?.remove();
  replayView.identity = "";
}

async function replayOpen(identity) {
  replayClose();
  const overlay = document.createElement("div");
  overlay.id = "rv-overlay";
  overlay.innerHTML = `<div class="rv-panel">
    <header class="rv-head">
      <strong>录像回放</strong><span id="rv-meta" class="muted">加载中…</span>
      <span class="rv-controls">
        <button id="rv-first" class="icon-button" data-tooltip="回到开头">${icon("skip-back")}</button>
        <button id="rv-prev-decision" class="icon-button" data-tooltip="上一个决策">${icon("rewind")}</button>
        <button id="rv-prev" class="icon-button" data-tooltip="上一帧">${icon("chevron-left")}</button>
        <button id="rv-play" class="primary icon-button" data-tooltip="播放/暂停">${icon("play")}</button>
        <button id="rv-next" class="icon-button" data-tooltip="下一帧">${icon("chevron-right")}</button>
        <button id="rv-next-decision" class="icon-button" data-tooltip="下一个决策">${icon("fast-forward")}</button>
        <button id="rv-last" class="icon-button" data-tooltip="跳到结尾">${icon("skip-forward")}</button>
        <select id="rv-speed" class="rv-speed"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option><option value="4">4×</option></select>
        <button id="rv-close" class="icon-button" data-tooltip="关闭">${icon("x")}</button>
      </span>
    </header>
    <div id="rv-score" class="rv-score"></div>
    <div class="rv-body">
      <div id="rv-board" class="rv-board"></div>
      <aside class="rv-log-wrap"><div id="rv-log" class="rv-log"></div></aside>
    </div>
    <footer class="rv-foot">
      <p id="rv-action" class="rv-action"></p>
      <input id="rv-range" class="rv-range" type="range" min="0" max="0" value="0">
    </footer>
  </div>`;
  document.body.append(overlay);
  icons();
  $("#rv-close").onclick = replayClose;
  $("#rv-play").onclick = () => replayToggle();
  $("#rv-first").onclick = () => replayShow(0);
  $("#rv-last").onclick = () => replayShow(replayView.frames.length - 1);
  $("#rv-prev").onclick = () => replayStep(-1);
  $("#rv-next").onclick = () => replayStep(1);
  $("#rv-prev-decision").onclick = () => replayDecisions(-1);
  $("#rv-next-decision").onclick = () => replayDecisions(1);
  $("#rv-speed").onchange = event => { replayView.speed = Number(event.target.value) || 1; };
  $("#rv-range").oninput = event => { replayToggle(false); replayShow(Number(event.target.value)); };
  $("#rv-log").onclick = event => {
    const row = event.target.closest("[data-jump]");
    if (row) { replayToggle(false); replayShow(Number(row.dataset.jump)); }
  };
  overlay.addEventListener("click", event => { if (event.target === overlay) replayClose(); });

  try {
    const data = await api("outbound/" + identity + "/playback");
    replayView.frames = data.frames || [];
    replayView.identity = identity;
    if (!replayView.frames.length) {
      $("#rv-meta").textContent = "这一局没有可播放的记录（未进入对局）";
      return;
    }
    const codes = new Set();
    for (const frame of replayView.frames) {
      for (const seat of ["0", "1"]) {
        for (const card of [...(frame.mon[seat] || []), ...(frame.st[seat] || [])]) {
          if (card && card.c) codes.add(card.c);
        }
      }
      if (frame.act && frame.act.code) codes.add(frame.act.code);
    }
    const ids = [...codes];
    for (let position = 0; position < ids.length; position += 200) {
      try {
        const result = await api("cards?ids=" + ids.slice(position, position + 200).join(","));
        for (const row of result.items || []) replayView.names.set(row.id, row.name);
      } catch (error) { /* names are cosmetic */ }
    }
    for (const frame of replayView.frames) frame.seat_model = data.model_seat;
    $("#rv-range").max = String(replayView.frames.length - 1);
    replayShow(0);
    replayToggle(true);
  } catch (error) {
    $("#rv-meta").textContent = error.message;
  }
}

document.addEventListener("click", event => {
  const trigger = event.target.closest("[data-play]");
  if (trigger) { event.preventDefault(); replayOpen(trigger.dataset.play).catch(failure); }
});

document.addEventListener("keydown", event => {
  if (!$("#rv-overlay")) return;
  if (event.key === "Escape") replayClose();
  else if (event.key === " ") { event.preventDefault(); replayToggle(); }
  else if (event.key === "ArrowRight") replayStep(1);
  else if (event.key === "ArrowLeft") replayStep(-1);
});

"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));

const query = new URLSearchParams(location.search);
const state = {
  room: null,
  mode: query.get("mode") === "admin"
    ? "admin"
    : query.get("mode") === "player" ? "player" : "spectator",
  adminRequired: true,
  adminKey: sessionStorage.getItem("tournament-admin-key") || "",
  playerToken: sessionStorage.getItem("tournament-player-token") || "",
  player: null,
  playerAccess: null,
  finalOpponentAccess: null,
  nativeSpectator: null,
  nativeAccess: null,
  adjudicationDrafts: {},
  forceFinishDrafts: {},
  socket: null,
  poll: null,
  httpOnline: null,
};

const mountPath = location.pathname === "/tournament"
  || location.pathname.startsWith("/tournament/")
  ? "/tournament"
  : "";
const appPath = path => `${mountPath}${path}`;

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 3200);
}

async function copyText(value) {
  const text = String(value ?? "");
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (_error) {
      // Fall through for browsers that expose but deny the Clipboard API.
    }
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.opacity = "0";
  input.style.pointerEvents = "none";
  document.body.append(input);
  input.focus();
  input.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } finally {
    input.remove();
  }
  if (!copied) throw new Error("copy_failed");
}

async function copyWithToast(value, successMessage) {
  try {
    await copyText(value);
    toast(successMessage);
  } catch (_error) {
    toast("复制失败，请手动选择并复制");
  }
}

function safeFilePart(value) {
  return String(value || "tournament").replace(/[^a-zA-Z0-9._-]+/g, "_");
}

function saveBlob(blob, filename) {
  const link = document.createElement("a");
  const url = URL.createObjectURL(blob);
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function playerConnectionCodes(room) {
  if (!state.player) return [];
  return [...new Set(room.matches
    .filter(match => playerSide(match))
    .map(match => match.session?.connection?.code)
    .filter(Boolean))];
}

function downloadConnectionCodebook(room) {
  const codes = playerConnectionCodes(room);
  if (!codes.length) {
    toast("当前还没有可用连接码");
    return;
  }
  saveBlob(
    new Blob([`${codes.join("\r\n")}\r\n`], {
      type: "text/plain;charset=utf-8",
    }),
    `${safeFilePart(room.id)}-connection-codes.txt`,
  );
  toast(`已下载 ${codes.length} 个连接码`);
}

async function downloadApi(path, fallbackName) {
  const headers = {};
  if (state.mode === "admin" && state.adminKey) {
    headers["X-Tournament-Admin-Key"] = state.adminKey;
  }
  if (state.mode === "player" && state.playerToken) {
    headers["X-Tournament-Player-Token"] = state.playerToken;
  }
  const response = await fetch(path, { headers });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `下载失败 (${response.status})`);
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const matched = disposition.match(/filename="([^"]+)"/i);
  saveBlob(await response.blob(), matched?.[1] || fallbackName);
}

function statusLabel(status) {
  return ({
    lobby: "等待开始",
    running: "进行中",
    paused: "已暂停",
    finished: "已结束",
    closed: "已关闭",
  }[status] || status || "未知");
}

function elapsedLabel(timestamp) {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return "";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - value));
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
}

function playerConnectionState(match, side) {
  const monitor = match?.session?.connection_status;
  if (!monitor) {
    return { state: "syncing", label: "状态同步中", detail: "正在读取原生连接" };
  }
  if (monitor.status === "unavailable") {
    return { state: "unavailable", label: "状态暂不可用", detail: "连接服务未响应" };
  }
  const player = monitor.players?.[side] || {};
  const state = ["waiting", "online", "reconnecting", "offline", "stalled"]
    .includes(player.state) ? player.state : "waiting";
  if (state === "online") {
    const elapsed = elapsedLabel(player.last_activity_at);
    return {
      state,
      label: "在线",
      detail: elapsed && elapsed !== "刚刚" ? `${elapsed}前有通信` : "实时通信",
    };
  }
  if (state === "stalled") {
    const elapsed = elapsedLabel(player.last_activity_at);
    return {
      state,
      label: "疑似卡住",
      detail: elapsed ? `${elapsed}无通信` : "长时间无通信",
    };
  }
  if (state === "reconnecting") {
    const elapsed = elapsedLabel(player.disconnected_at);
    return {
      state,
      label: "已断线，等待重连",
      detail: elapsed ? `已等待 ${elapsed}` : "重连入口仍开放",
    };
  }
  if (state === "offline") {
    const elapsed = elapsedLabel(player.disconnected_at);
    return {
      state,
      label: "已退出 / 断线",
      detail: elapsed ? `${elapsed}前断开` : "当前未连接",
    };
  }
  return { state: "waiting", label: "尚未连接", detail: "等待选手进入" };
}

function renderPlayerConnectionState(match, side) {
  const info = playerConnectionState(match, side);
  return `<span class="player-connection-state ${esc(info.state)}">
    <b>${esc(info.label)}</b><small>${esc(info.detail)}</small>
  </span>`;
}

function phaseLabel(match) {
  if (!match) return "暂无对局";
  if (match.phase === "tiebreak") return "换位加赛";
  if (match.phase === "finished") return "已完成";
  if (match.phase === "closed") return "已关闭";
  return match.stage === "final" ? "决赛"
    : match.stage === "series" ? "系列赛" : "循环赛";
}

function replayPhaseLabel(match) {
  const replay = match?.replay_status || {};
  const count = Number(replay.replay_count || 0);
  const expected = Number(
    replay.expected_replay_count || match?.total_game_count || 0
  );
  const progress = expected ? ` ${count}/${expected}` : "";
  if (replay.available === true) {
    return replay.complete === true ? "录像" : `部分录像${progress}`;
  }
  return `录像不完整${progress}`;
}

function stageLabel(stage) {
  return stage === "final" ? "决赛"
    : stage === "series" ? "系列赛"
      : stage === "closed" ? "已关闭"
      : stage === "finished" ? "已结束" : "循环赛";
}

function matchLabel(match) {
  return `${match.player_a_name} vs ${match.player_b_name}`;
}

function seatLabel(plan) {
  return plan === "a-first" ? "A 方先手"
    : plan === "b-first" ? "B 方先手"
      : plan === "swap-block" ? "本组换位"
        : "待定";
}

function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (state.mode === "admin" && state.adminKey) {
    headers["X-Tournament-Admin-Key"] = state.adminKey;
  }
  if (state.mode === "player" && state.playerToken) {
    headers["X-Tournament-Player-Token"] = state.playerToken;
  }
  return fetch(path, { ...options, headers }).then(async response => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
    return data;
  });
}

function renderHome() {
  closeSocket();
  state.room = null;
  setConnection(null);
  $("#app").innerHTML = `
    <section class="hero">
      <div>
        <p class="eyebrow">YGO TOURNAMENT</p>
        <h1>赛事房间</h1>
        <p class="hero-copy">固定卡组赛事系统，可一次生成全部循环赛对局房间，实时记分、公开观战并自动续局。</p>
      </div>
      <div class="hero-rule">
        <strong>2</strong><span>每个 BO2 换位局数</span>
        <strong>3</strong><span>胜方积分</span>
      </div>
    </section>
    <section class="home-grid">
      <form id="create-form" class="panel">
        <div class="panel-head"><div><p class="eyebrow">ADMIN</p><h2>创建赛事房间</h2></div><span class="pill" id="room-type-pill">双人系列赛</span></div>
        <label>房间名称<input name="name" maxlength="40" placeholder="闪刀镜像测试赛" required></label>
        <label>房间类型<select name="room_type" id="room-type">
          <option value="series" selected>双人连续系列赛</option>
          <option value="league">K 人循环联赛（3–32 人）</option>
        </select></label>
        <label id="player-count-field" hidden>赛程人数<input name="player_count" id="player-count" type="number" min="3" max="32" value="3" required><small>支持 3–32 人，系统自动生成 K×(K−1)/2 个对位。</small></label>
        <label>选手名单 <span class="label-note" id="player-note">每行一人，双人房间固定两名选手</span>
          <textarea name="players" id="player-names" rows="2" placeholder="选手 A&#10;选手 B" required></textarea>
        </label>
        <label id="final-opponent-field" hidden>循环赛冠军挑战者<input name="final_opponent_name" maxlength="40" placeholder="例如：真人选手"><small>循环赛结束后，积分第一名与该真人进行 BO7 决赛。</small></label>
        <label id="final-completion-field" hidden>冠军真人 BO7 结束方式<select name="final_completion_policy">
          <option value="first_to_four" selected>先赢 4 局立即结束</option>
          <option value="play_all_seven">不管胜负固定打满 7 局</option>
        </select><small>固定七局模式按胜局数决出冠军；七局后胜场相同则继续单局加赛。</small></label>
        <div id="series-options" class="form-options">
          <label>赛制<select name="series_format" id="series-format">
            <option value="bo2" selected>BO2（每组 2 局）</option>
            <option value="games">按实际局数</option>
          </select></label>
          <label id="bo2-count-field"><span id="bo2-count-label">BO2 组数</span><input name="base_bo2_count" id="bo2-count" type="number" min="1" max="100" value="10" required><small id="bo2-total">共 20 局（打平加赛除外）</small></label>
          <label id="game-count-field" hidden><span id="game-count-label">实际局数</span><input name="game_count" id="game-count" type="number" min="1" max="200" value="10" disabled><small>每局新种子，双方逐局轮换先手；奇数局先手次数相差 1</small></label>
          <label id="seed-mode-field">种子模式<select name="seed_mode">
            <option value="mirrored" selected>同种子换位</option>
            <option value="independent">异种子换位</option>
          </select></label>
          <label id="overtime-field" class="check-row"><input name="overtime_enabled" type="checkbox" checked><span>积分打平时启用双胜加赛</span></label>
          <label class="check-row"><input name="auto_next_game" type="checkbox" checked><span>每局结束后自动建立下一局</span></label>
        </div>
        <label>牌局引擎<select name="engine">
          <option value="native-pvp" selected>MDPro3 / YGOPro2 真人对局</option>
          <option value="manual-admin">管理员手工记分</option>
        </select></label>
        ${state.adminRequired
          ? `<label>管理员密钥<input name="key" type="password" value="${esc(state.adminKey)}" autocomplete="off" placeholder="服务启动时配置的密钥" required></label>`
          : ""}
        <button class="primary" type="submit">创建房间</button>
        <p class="form-error" id="create-error"></p>
      </form>
      <section class="panel">
        <div class="panel-head"><div><p class="eyebrow">SPECTATOR</p><h2>进入观战</h2></div><span class="pill">只读</span></div>
        <form id="watch-form" class="watch-form">
          <label>房间编号<input name="room" placeholder="例如 r-a1b2c3d4" required></label>
          <button type="submit">打开观战面板</button>
        </form>
        <div class="room-list">
          <div class="list-head"><span>最近房间</span><button id="refresh-rooms" class="link-btn" type="button">刷新</button></div>
          <div id="rooms"></div>
        </div>
      </section>
      <section class="panel player-entry">
        <div class="panel-head"><div><p class="eyebrow">PLAYER ENTRY</p><h2>选手进入房间</h2></div><span class="pill">专属令牌</span></div>
        <form id="join-form" class="watch-form">
          <label>房间编号<input name="room" placeholder="例如 r-a1b2c3d4" required></label>
          <label>选手令牌<input name="token" type="password" autocomplete="off" placeholder="管理员发给你的入口令牌" required></label>
          <button class="primary" type="submit">进入选手席</button>
        </form>
        <p class="form-hint">令牌只绑定一个固定席位，不能代替其他选手进入。</p>
        <p class="form-error" id="join-error"></p>
      </section>
    </section>`;
  $("#create-form").addEventListener("submit", createRoom);
  $("#room-type").addEventListener("change", updateRoomType);
  $("#series-format").addEventListener("change", updateSeriesFormat);
  $("#bo2-count").addEventListener("input", updateSeriesFormat);
  $("#player-count").addEventListener("input", updatePlayerCountHint);
  updateRoomType();
  updateSeriesFormat();
  $("#watch-form").addEventListener("submit", event => {
    event.preventDefault();
    goRoom(new FormData(event.currentTarget).get("room"), "spectator");
  });
  $("#join-form").addEventListener("submit", joinRoom);
  $("#refresh-rooms").addEventListener("click", loadRooms);
  loadRooms();
}

function updateRoomType() {
  const type = $("#room-type")?.value || "series";
  const league = type === "league";
  $("#player-count-field").hidden = !league;
  $("#final-opponent-field").hidden = !league;
  $("#final-completion-field").hidden = !league;
  $("#series-options").hidden = false;
  $("#room-type-pill").textContent = league ? "K 人循环赛" : "双人系列赛";
  $("#bo2-count-label").textContent = league ? "每个对局房间的 BO2 组数" : "BO2 组数";
  $("#game-count-label").textContent = league ? "每个对局房间的实际局数" : "实际局数";
  $("#player-note").textContent = league
    ? "每行一人，数量必须匹配赛程人数"
    : "每行一人，双人房间固定两名选手";
  updatePlayerCountHint();
  updateSeriesFormat();
}

function updateSeriesFormat() {
  const league = $("#room-type")?.value === "league";
  const games = $("#series-format").value === "games";
  $("#bo2-count-field").hidden = games;
  $("#bo2-count").disabled = games;
  $("#bo2-count").required = !games;
  $("#game-count-field").hidden = !games;
  $("#game-count").disabled = !games;
  $("#game-count").required = games;
  $("#seed-mode-field").hidden = games;
  $("#seed-mode-field select").disabled = games;
  $("#overtime-field").hidden = games || league;
  $("#overtime-field input").disabled = games || league;
  $("#overtime-field input").checked = !games && !league;
  const total = Number($("#bo2-count").value || 0) * 2;
  $("#bo2-total").textContent = league
    ? `每个配对房间共 ${total} 局，联赛对位允许平局`
    : `共 ${total} 局（打平加赛除外）`;
}

function updatePlayerCountHint() {
  const select = $("#player-count");
  const names = $("#player-names");
  if (!select || !names) return;
  const count = $("#room-type")?.value === "league"
    ? Number(select.value) : 2;
  names.rows = count;
  names.placeholder = Array.from(
    { length: count },
    (_, index) => `选手 ${String.fromCharCode(65 + index)}`
  ).join("\n");
}

async function loadRooms() {
  try {
    const data = await api(appPath("/api/rooms"));
    const target = $("#rooms");
    if (!target) return;
    setConnection(true);
    target.innerHTML = data.items.length ? data.items.map(room => `
      <button class="room-row" data-room="${esc(room.id)}">
        <span><strong>${esc(room.name)}</strong><small>${esc(room.id)} · ${room.room_type === "series" ? "双人系列赛" : `${room.player_count} 人联赛`}</small></span>
        <span class="room-status ${esc(room.status)}">${esc(statusLabel(room.status))}</span>
      </button>`).join("") : `<p class="empty">暂无赛事房间</p>`;
    target.querySelectorAll("[data-room]").forEach(button => {
      button.addEventListener("click", () => goRoom(button.dataset.room, "spectator"));
    });
  } catch (error) {
    const target = $("#rooms");
    if (target) {
      setConnection(false);
      target.innerHTML = `<p class="empty">${esc(error.message)}</p>`;
    }
  }
}

async function createRoom(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const error = $("#create-error");
  error.textContent = "";
  const data = new FormData(form);
  state.adminKey = state.adminRequired
    ? String(data.get("key") || "").trim()
    : "";
  state.mode = "admin";
  if (state.adminKey) {
    sessionStorage.setItem("tournament-admin-key", state.adminKey);
  } else {
    sessionStorage.removeItem("tournament-admin-key");
  }
  try {
    const room = await api(appPath("/api/rooms"), {
      method: "POST",
      body: JSON.stringify({
        name: data.get("name"),
        room_type: data.get("room_type") || "series",
        player_count: data.get("room_type") === "league"
          ? Number(data.get("player_count")) : 2,
        players: String(data.get("players") || "").split(/\r?\n/),
        engine: data.get("engine") || "native-pvp",
        base_bo2_count: Number(data.get("base_bo2_count") || 10),
        series_format: data.get("series_format") || "bo2",
        game_count: data.has("game_count") ? Number(data.get("game_count")) : undefined,
        seed_mode: data.get("seed_mode") || "mirrored",
        overtime_enabled: data.has("overtime_enabled"),
        auto_next_game: data.has("auto_next_game"),
        final_opponent_name: String(data.get("final_opponent_name") || "").trim() || undefined,
        final_completion_policy: data.get("final_completion_policy") || "first_to_four",
      }),
    });
    state.playerAccess = room.player_access || null;
    state.finalOpponentAccess = room.final_opponent_access || null;
    goRoom(room.id, "admin");
  } catch (caught) {
    error.textContent = caught.message;
  }
}

async function joinRoom(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const error = $("#join-error");
  error.textContent = "";
  const data = new FormData(form);
  const token = String(data.get("token") || "").trim();
  const roomId = String(data.get("room") || "").trim();
  state.playerToken = token;
  state.mode = "player";
  sessionStorage.setItem("tournament-player-token", token);
  try {
    if (!roomId) throw new Error("请输入房间编号");
    const joined = await api(appPath(`/api/rooms/${encodeURIComponent(roomId)}/join`), {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    state.player = joined.player;
    goRoom(roomId, "player");
  } catch (caught) {
    error.textContent = caught.message;
  }
}

function goRoom(roomId, mode) {
  if (state.room?.id !== roomId) {
    state.playerAccess = null;
    state.finalOpponentAccess = null;
    state.nativeAccess = null;
    state.nativeSpectator = null;
  }
  const params = new URLSearchParams({ room: roomId, mode });
  history.pushState({}, "", `${mountPath}/?${params}`);
  state.mode = mode;
  loadRoom(roomId);
}

async function loadRoom(roomId) {
  closeSocket();
  setConnection(null);
  try {
    if (state.mode === "player" && state.playerToken) {
      const joined = await api(appPath(`/api/rooms/${encodeURIComponent(roomId)}/join`), {
        method: "POST",
        body: JSON.stringify({ token: state.playerToken }),
      });
      state.player = joined.player;
    }
    state.room = await api(appPath(`/api/rooms/${encodeURIComponent(roomId)}`));
    setConnection(true);
    renderRoom();
    connectSocket(roomId);
    clearInterval(state.poll);
    state.poll = setInterval(() => refreshRoom(roomId), 4000);
  } catch (error) {
    toast(error.message);
    history.replaceState({}, "", `${mountPath}/`);
    state.room = null;
    renderHome();
  }
}

async function refreshRoom(roomId) {
  try {
    const room = await api(appPath(`/api/rooms/${encodeURIComponent(roomId)}`));
    if (state.room?.id !== roomId) return;
    state.room = room;
    setConnection(true);
    renderRoom();
  } catch (_) {
    if (state.room?.id === roomId) setConnection(false);
  }
}

function setConnection(online = state.httpOnline) {
  state.httpOnline = online;
  const realtime = navigator.onLine
    && state.socket?.readyState === WebSocket.OPEN;
  const connected = navigator.onLine && (realtime || online === true);
  const checking = navigator.onLine && online === null && !realtime;
  document.querySelectorAll("[data-connection-status]").forEach(node => {
    node.textContent = realtime ? "实时连接" : connected ? "在线"
      : checking ? "连接中" : "连接中断";
    node.className = `status-dot${connected ? " online" : checking ? "" : " offline"}`;
  });
}

function connectSocket(roomId) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const token = state.mode === "player" && state.playerToken
    ? `?token=${encodeURIComponent(state.playerToken)}`
    : "";
  const socket = new WebSocket(
    `${scheme}://${location.host}${appPath(`/ws/${encodeURIComponent(roomId)}`)}${token}`
  );
  state.socket = socket;
  socket.addEventListener("open", () => {
    if (state.socket === socket) setConnection(true);
  });
  socket.addEventListener("message", event => {
    if (state.socket !== socket) return;
    if (event.data === "pong") return;
    try {
      state.room = JSON.parse(event.data);
      renderRoom();
    } catch (_) {}
  });
  socket.addEventListener("close", () => {
    if (state.socket !== socket) return;
    state.socket = null;
    refreshRoom(roomId);
  });
  socket.addEventListener("error", () => {
    if (state.socket === socket) setConnection(false);
  });
}

function closeSocket() {
  const socket = state.socket;
  state.socket = null;
  if (socket) socket.close();
  clearInterval(state.poll);
  state.poll = null;
}

function renderRoom() {
  const room = state.room;
  if (!room) return renderHome();
  const admin = state.mode === "admin";
  const playerMode = state.mode === "player";
  const parallelLeague = room.room_type === "league"
    && room.rules.parallel_matches;
  const showParallelRooms = parallelLeague && room.status !== "closed";
  const finishedReplay = room.status === "finished"
    ? [...room.matches].reverse().find(
      match => match.status === "finished" && match.observer_connection
    )
    : null;
  const active = room.active_match || (!parallelLeague ? finishedReplay : null);
  if (state.nativeSpectator && !room.matches.some(
    match => match.id === state.nativeSpectator.match_id
  )) {
    state.nativeSpectator = null;
  }
  const standings = room.standings || [];
  const finishedMatches = room.matches.filter(match => match.status === "finished").length;
  const connectionCodeCount = playerMode
    ? playerConnectionCodes(room).length : 0;
  const replayMatches = room.matches.filter(match => (
    match.status === "finished"
    && match.session?.pvp_session_id
    && (admin || (playerMode && playerSide(match)))
  ));
  const finalRuleText = room.final_opponent
    ? room.rules.final_completion_policy === "play_all_seven"
      ? "循环赛第一名再与真人固定打满 7 局，胜局多者夺冠；同分继续单局加赛。"
      : "循环赛第一名再与真人进行 BO7，先赢 4 局立即结束。"
    : "循环赛结束后前两名进行旧版模型决赛。";
  $("#app").innerHTML = `
    <section class="room-header">
      <div>
        <button id="back" class="back-btn" type="button">← 房间列表</button>
        <p class="eyebrow">${admin ? "ADMIN CONTROL" : playerMode ? "PLAYER LOBBY" : "LIVE SPECTATOR"}</p>
        <h1>${esc(room.name)}</h1>
        <p class="room-meta">${esc(room.id)} · ${room.room_type === "series" ? "双人系列赛" : `${room.player_count || room.players.length} 人联赛`} · ${esc(statusLabel(room.status))}</p>
      </div>
      <div class="room-actions">
        <span data-connection-status class="status-dot">连接中</span>
        <button id="copy-link" type="button">复制观战链接</button>
        ${playerMode ? `<button id="download-codebook" type="button" ${connectionCodeCount ? "" : "disabled"}>下载连接码本</button>` : ""}
        ${(admin || playerMode) && room.engine === "native-pvp"
          ? `<button id="download-all-replays" type="button" ${replayMatches.length ? "" : "disabled"}>${playerMode ? "下载我的录像" : "下载全部录像"}</button>`
          : ""}
        ${admin ? room.status === "lobby"
          ? `<button id="start-room" class="primary" type="button">开始比赛</button>`
          : room.status === "paused"
            ? `<button id="resume-room" class="primary" type="button">恢复比赛</button>`
            : room.status === "running"
              ? `<button id="pause-room" type="button">暂停</button>` : "" : ""}
        ${admin && room.status !== "closed"
          ? `<button id="close-room" class="danger" type="button">关闭房间</button>` : ""}
        ${admin ? `${room.status !== "closed" ? `<button id="rotate-access" type="button">重新生成入口</button>
          ${room.engine === "native-pvp" ? `<button id="native-access" type="button">比赛连接信息</button><button id="native-spectator" type="button">观战/回放参数</button>` : ""}` : ""}
          <button id="spectator-mode" type="button">切换观战</button>`
          : playerMode ? `<button id="spectator-mode" type="button">切换观战</button>`
            : `<button id="admin-mode" type="button">管理员模式</button>`}
      </div>
    </section>
    ${admin && (state.playerAccess || state.finalOpponentAccess) ? renderPlayerAccess(room) : ""}
    ${admin && state.nativeAccess ? renderNativeAccess(state.nativeAccess, room) : ""}
    ${admin && state.nativeSpectator ? renderNativeSpectator(state.nativeSpectator) : ""}
    <section class="scoreboard-grid ${showParallelRooms ? "single-column" : ""}">
      <section class="panel standings-panel">
        <div class="panel-head">
          <div><p class="eyebrow">STANDINGS</p><h2>实时积分</h2></div>
          <span class="pill">${room.status === "closed"
            ? "已关闭" : `${esc(stageLabel(room.stage))} · ${esc(statusLabel(room.status))}`}</span>
        </div>
        ${renderStandings(standings, room)}
        <p class="table-note">${room.room_type === "series"
          ? room.rules.series_format === "games"
            ? `规则：共 ${room.rules.game_count} 局，每局新种子、轮换先手；每局胜者 3 分，平局各 1 分；打满即结束。`
            : `规则：${room.rules.base_bo2_count} 个 BO2，共 ${room.rules.base_bo2_count * 2} 局${room.rules.overtime_enabled ? "（打平可加赛）" : ""}；每组两局换位，胜者 3 分，平局各 1 分；${room.rules.seed_mode === "mirrored" ? "同种子" : "异种子"}。`
          : room.rules.series_format === "games"
            ? `规则：${room.player_count} 人循环赛，一次开放 ${room.matches.filter(match => match.stage === "round_robin").length} 个独立对局房间；每个房间打 ${room.rules.game_count} 局，每局胜 3 分、平局各 1 分。${finalRuleText}`
            : `规则：${room.player_count} 人循环赛，一次开放 ${room.matches.filter(match => match.stage === "round_robin").length} 个独立对局房间；每个房间 ${room.rules.base_bo2_count} 个 BO2、共 ${room.rules.base_bo2_count * 2} 局；BO2 胜 3 分、平局各 1 分。${finalRuleText}`}</p>
      </section>
      ${showParallelRooms ? "" : `<section class="panel active-panel">
        <div class="panel-head">
          <div><p class="eyebrow">CURRENT MATCH</p><h2>${active
            ? esc(matchLabel(active)) : room.status === "closed" ? "房间已关闭" : "等待开始"}</h2></div>
          ${active ? `<span class="phase ${active.phase}">${active.status === "finished"
            ? esc(replayPhaseLabel(active)) : esc(phaseLabel(active))}</span>` : ""}
        </div>
        ${active ? renderActiveMatch(room, active, admin, playerMode) : renderNoActive(room)}
      </section>`}
    </section>
    ${showParallelRooms ? renderMatchRooms(room, admin, playerMode) : ""}
    <section class="panel schedule-panel">
      <div class="panel-head">
        <div><p class="eyebrow">SCHEDULE</p><h2>完整赛程</h2></div>
        <span class="muted">${finishedMatches} / ${room.matches.length} 个对位完成</span>
      </div>
      ${renderSchedule(room)}
    </section>`;

  setConnection();
  $("#back").addEventListener("click", () => {
    closeSocket();
    history.pushState({}, "", `${mountPath}/`);
    state.room = null;
    renderHome();
  });
  $("#copy-link").addEventListener("click", () => copyWithToast(
    `${location.origin}${mountPath}${room.spectator_url}`,
    "观战链接已复制"
  ));
  const codebook = $("#download-codebook");
  if (codebook) codebook.addEventListener(
    "click", () => downloadConnectionCodebook(room)
  );
  const allReplays = $("#download-all-replays");
  if (allReplays) allReplays.addEventListener("click", async () => {
    try {
      await downloadApi(
        appPath(`/api/rooms/${room.id}/replays`),
        `${safeFilePart(room.id)}-replays.zip`,
      );
    } catch (error) {
      toast(error.message);
    }
  });
  const start = $("#start-room");
  if (start) start.addEventListener("click", () => mutate(appPath(`/api/rooms/${room.id}/start`)));
  const pause = $("#pause-room");
  if (pause) pause.addEventListener("click", () => mutate(appPath(`/api/rooms/${room.id}/pause`)));
  const resume = $("#resume-room");
  if (resume) resume.addEventListener("click", () => mutate(appPath(`/api/rooms/${room.id}/resume`)));
  const close = $("#close-room");
  if (close) close.addEventListener("click", closeRoom);
  const adminMode = $("#admin-mode");
  if (adminMode) adminMode.addEventListener("click", () => switchMode("admin"));
  const spectatorMode = $("#spectator-mode");
  if (spectatorMode) spectatorMode.addEventListener("click", () => switchMode("spectator"));
  const rotateAccess = $("#rotate-access");
  if (rotateAccess) rotateAccess.addEventListener("click", rotatePlayerAccess);
  const nativeSpectator = $("#native-spectator");
  if (nativeSpectator) nativeSpectator.addEventListener("click", loadNativeSpectator);
  const nativeAccess = $("#native-access");
  if (nativeAccess) nativeAccess.addEventListener("click", loadNativeAccess);
  document.querySelectorAll("[data-copy-player-url]").forEach(button => {
    button.addEventListener("click", () => copyWithToast(
      buildAppUrl(button.dataset.copyPlayerUrl),
      "选手入口已复制"
    ));
  });
  document.querySelectorAll("[data-copy-value]").forEach(button => {
    button.addEventListener("click", () => copyWithToast(
      button.dataset.copyValue || "",
      button.dataset.copyMessage || "连接信息已复制"
    ));
  });
  document.querySelectorAll("[data-match-winner]").forEach(button => {
    button.addEventListener("click", () => recordGame(
      button.dataset.matchWinner,
      button.dataset.winner
    ));
  });
  document.querySelectorAll("[data-undo-match]").forEach(button => {
    button.addEventListener("click", () => mutate(
      appPath(`/api/rooms/${room.id}/matches/${button.dataset.undoMatch}/undo`)
    ));
  });
  document.querySelectorAll("[data-adjudication-form]").forEach(form => {
    const matchId = form.dataset.adjudicationForm;
    const saveDraft = () => {
      const data = new FormData(form);
      state.adjudicationDrafts[matchId] = {
        ...(state.adjudicationDrafts[matchId] || {}),
        penalizedSide: data.get("penalized_side"),
        scope: data.get("scope"),
        reason: String(data.get("reason") || ""),
      };
    };
    form.addEventListener("input", saveDraft);
    form.addEventListener("change", saveDraft);
    form.addEventListener("submit", event => {
      event.preventDefault();
      adjudicateMatch(form);
    });
  });
  document.querySelectorAll("[data-adjudication-details]").forEach(details => {
    details.addEventListener("toggle", () => {
      const matchId = details.dataset.adjudicationDetails;
      state.adjudicationDrafts[matchId] = {
        ...(state.adjudicationDrafts[matchId] || {}),
        open: details.open,
      };
    });
  });
  document.querySelectorAll("[data-force-finish-form]").forEach(form => {
    const matchId = form.dataset.forceFinishForm;
    const saveDraft = () => {
      const data = new FormData(form);
      state.forceFinishDrafts[matchId] = {
        ...(state.forceFinishDrafts[matchId] || {}),
        pointsA: String(data.get("points_a") || "0"),
        pointsB: String(data.get("points_b") || "0"),
        reason: String(data.get("reason") || ""),
      };
    };
    form.addEventListener("input", saveDraft);
    form.addEventListener("change", saveDraft);
    form.addEventListener("submit", event => {
      event.preventDefault();
      forceFinishMatch(form);
    });
  });
  document.querySelectorAll("[data-force-finish-details]").forEach(details => {
    details.addEventListener("toggle", () => {
      const matchId = details.dataset.forceFinishDetails;
      state.forceFinishDrafts[matchId] = {
        ...(state.forceFinishDrafts[matchId] || {}),
        open: details.open,
      };
    });
  });
  document.querySelectorAll("[data-undo-adjudication]").forEach(button => {
    button.addEventListener("click", () => undoAdjudication(
      button.dataset.undoAdjudication
    ));
  });
  document.querySelectorAll("[data-ready-match]").forEach(button => {
    button.addEventListener("click", () => playerReady(
      button.dataset.readyMatch
    ));
  });
  document.querySelectorAll("[data-native-spectator-match]").forEach(button => {
    button.addEventListener("click", () => loadNativeSpectator(
      button.dataset.nativeSpectatorMatch
    ));
  });
  document.querySelectorAll("[data-replay-match]").forEach(button => {
    button.addEventListener("click", async () => {
      try {
        const matchId = button.dataset.replayMatch;
        await downloadApi(
          appPath(`/api/rooms/${room.id}/matches/${matchId}/replays`),
          `${safeFilePart(room.id)}-${safeFilePart(matchId)}-replays.zip`,
        );
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

function renderStandings(rows, room) {
  const singleGames = room.rules.series_format === "games";
  return `<div class="table-wrap"><table class="standings"><thead><tr>
    <th>#</th><th>选手</th><th>积分</th><th>对位</th><th>${singleGames ? "单局胜 / 负 / 平" : "BO2 胜 / 负 / 平"}</th><th>局差</th>
  </tr></thead><tbody>${rows.map(row => `
    <tr class="${row.rank === 1 ? "leader" : ""}">
      <td class="rank">${row.rank}</td><td><strong>${esc(row.name)}</strong></td>
      <td class="points">${row.match_points}</td><td>${row.matches_played}</td>
      <td>${singleGames
        ? `${row.game_wins} / ${row.game_losses} / ${row.game_draws || 0}`
        : `${row.bo2_wins} / ${row.bo2_losses} / ${row.bo2_draws}`}</td>
      <td>${row.game_diff > 0 ? "+" : ""}${row.game_diff}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

function buildAppUrl(relative) {
  return `${location.origin}${mountPath}${relative}`;
}

function renderPlayerAccess(room) {
  const entries = [
    ...(state.playerAccess || []),
    ...(state.finalOpponentAccess ? [state.finalOpponentAccess] : []),
  ];
  return `<section class="panel access-panel">
    <div class="panel-head"><div><p class="eyebrow">PLAYER LINKS</p><h2>选手专属入口</h2></div><span class="pill">仅显示一次</span></div>
    <div class="access-list">${entries.map(entry => `
      <div class="access-row">
        <span><strong>${esc(entry.name)}</strong><small>${entry.role === "final_opponent" || entry.player_id === "p-final" ? "真人决赛挑战者" : esc(entry.player_id)}</small></span>
        <code>${esc(entry.token)}</code>
        <button type="button" data-copy-player-url="${esc(entry.join_url)}">复制入口</button>
      </div>`).join("")}</div>
    <p class="form-hint">请分别发给循环赛选手和真人决赛挑战者；重新生成后旧入口立即失效。</p>
  </section>`;
}

function renderNativeSpectator(info) {
  const connection = info.connection || {};
  return `<section class="panel access-panel">
    <div class="panel-head"><div><p class="eyebrow">NATIVE OBSERVER</p><h2>原生观战连接</h2></div><span class="pill">仅管理员</span></div>
    <div class="access-list">
      <div class="access-row"><span><strong>服务器</strong><small>${esc(info.status || "waiting")}</small></span><code>${esc(connection.host)}:${esc(connection.port)}</code></div>
      <div class="access-row"><span><strong>观战码</strong><small>只发送给可信观战者</small></span><code>${esc(connection.code)}</code></div>
    </div>
    <p class="form-hint">在 MDPro3 / YGOPro2 中使用服务器、端口和观战密码加入当前对局。双人系列赛的观战密码整场保持不变。</p>
  </section>`;
}

function renderNativeAccess(info, room) {
  const entries = info.matches || [info];
  return `<section class="panel access-panel">
    <div class="panel-head"><div><p class="eyebrow">NATIVE ACCESS</p><h2>MDPro3 / YGOPro2 对局房间</h2></div><span class="pill">${entries.length} 个房间</span></div>
    <div class="native-access-groups">${entries.map((entry, index) => {
      const passwords = entry.player_passwords || {};
      const match = room.matches.find(item => item.id === entry.match_id)
        || room.active_match || {};
      const playerA = entry.player_a?.name || match.player_a_name || "选手 A";
      const playerB = entry.player_b?.name || match.player_b_name || "选手 B";
      const endpoint = entry.host && entry.port
        ? `${entry.host}:${entry.port}` : "房间开始后显示";
      return `<section class="native-access-group">
        <div class="native-access-title">
          <strong>房间 ${String(index + 1).padStart(2, "0")} · ${esc(playerA)} vs ${esc(playerB)}</strong>
          <code>${esc(entry.duel_room_id || entry.match_id || "")}</code>
        </div>
        <div class="access-list">
          <div class="access-row"><span><strong>主机与端口</strong><small>${esc(entry.status || "waiting")}</small></span><code>${esc(endpoint)}</code>${entry.host && entry.port ? `<button type="button" data-copy-value="${esc(endpoint)}">复制</button>` : ""}</div>
          <div class="access-row"><span><strong>${esc(playerA)}</strong><small>选手 A 密码</small></span><code>${esc(passwords.a)}</code><button type="button" data-copy-value="${esc(passwords.a)}">复制</button></div>
          <div class="access-row"><span><strong>${esc(playerB)}</strong><small>选手 B 密码</small></span><code>${esc(passwords.b)}</code><button type="button" data-copy-value="${esc(passwords.b)}">复制</button></div>
          <div class="access-row"><span><strong>观战者</strong><small>公开观战密码</small></span><code>${esc(entry.spectator_password)}</code><button type="button" data-copy-value="${esc(entry.spectator_password)}">复制</button></div>
        </div>
      </section>`;
    }).join("")}</div>
    <p class="form-hint">每个配对房间的密码相互独立，整场自动续局期间保持不变。</p>
  </section>`;
}

function playerSide(match) {
  if (!state.player) return null;
  if (match.player_a === state.player.id) return "a";
  if (match.player_b === state.player.id) return "b";
  return null;
}

function adjudicationDraft(match) {
  return {
    penalizedSide: "a",
    scope: "remaining",
    reason: "",
    open: false,
    ...(state.adjudicationDrafts[match.id] || {}),
  };
}

function forceFinishDraft(match) {
  const points = match.score || match.points || { a: 0, b: 0 };
  return {
    pointsA: String(points.a || 0),
    pointsB: String(points.b || 0),
    reason: "",
    open: false,
    ...(state.forceFinishDrafts[match.id] || {}),
  };
}

function renderActiveMatch(room, match, admin, playerMode) {
  const isTiebreak = match.phase === "tiebreak";
  const acceptingResults = match.status === "running" || match.status === "tiebreak";
  const singleGames = room.rules.series_format === "games"
    && match.stage !== "final";
  const firstToFour = match.match_format === "first_to_four";
  const playAllSeven = firstToFour
    && match.completion_policy === "play_all_seven";
  const points = match.score || match.points || { a: 0, b: 0 };
  const aScore = firstToFour
    ? points.a
    : isTiebreak ? match.playoff_block_wins.a : points.a;
  const bScore = firstToFour
    ? points.b
    : isTiebreak ? match.playoff_block_wins.b : points.b;
  const gameCount = isTiebreak || firstToFour
    ? match.total_game_count : match.regular_game_count;
  const bo2Text = match.status === "finished"
    ? `已完成 · 最终积分 ${points.a}:${points.b}`
    : match.status === "closed"
      ? "房间已关闭"
    : isTiebreak
    ? `加赛第 ${match.playoff_block || 1} 组 · ${match.playoff_block_progress || ""}`
    : firstToFour
      ? playAllSeven
        ? gameCount >= (match.max_games || 7)
          ? `固定 7 局已打满 · 单局加赛 · 当前胜场 ${points.a}:${points.b}`
          : `固定打满 ${match.max_games || 7} 局 · 当前胜场 ${points.a}:${points.b}`
        : `先到 ${match.target_wins || 4} 胜 · 最多 ${match.game_limit || match.max_games || 7} 局 · 当前比分 ${points.a}:${points.b}`
    : singleGames
      ? `第 ${match.next_game} / ${match.game_limit} 局 · 积分 ${points.a}:${points.b}`
      : `BO2 ${match.bo2_count + 1} / ${match.bo2_limit}（共 ${match.game_limit} 局） · 本阶段积分 ${points.a}:${points.b}`;
  const gameText = !acceptingResults
    ? `共完成 ${match.total_game_count || 0} 局`
    : isTiebreak
    ? `本组第 ${match.next_game ? ((match.next_game - 1) % 2) + 1 : "--"} 局`
    : firstToFour
      ? `第 ${match.next_game} 局 · 已完成 ${gameCount} 局`
    : singleGames ? `已完成 ${gameCount} 局` : `第 ${match.bo2_game || 1} 局 · 已完成 ${gameCount} 局`;
  const controls = admin && room.engine === "manual-admin"
    && room.status === "running" && acceptingResults ? `
    <div class="score-controls">
      <button class="score-button" data-match-winner="${esc(match.id)}" data-winner="a"><strong>${esc(match.player_a_name)}</strong><span>本局胜</span></button>
      <button class="draw-button" data-match-winner="${esc(match.id)}" data-winner="draw">平局</button>
      <button class="score-button" data-match-winner="${esc(match.id)}" data-winner="b"><strong>${esc(match.player_b_name)}</strong><span>本局胜</span></button>
    </div>
    <button data-undo-match="${esc(match.id)}" class="link-btn undo" type="button">撤销上一局</button>` : "";
  const rulingDraft = adjudicationDraft(match);
  const adjudicationControls = admin && room.status === "running"
    && acceptingResults ? `
    <details class="adjudication-controls" data-adjudication-details="${esc(match.id)}" ${rulingDraft.open ? "open" : ""}>
      <summary>判罚结算</summary>
      <form data-adjudication-form="${esc(match.id)}">
        <label>判负选手<select name="penalized_side">
          <option value="a" ${rulingDraft.penalizedSide === "a" ? "selected" : ""}>${esc(match.player_a_name)}</option>
          <option value="b" ${rulingDraft.penalizedSide === "b" ? "selected" : ""}>${esc(match.player_b_name)}</option>
        </select></label>
        <label>判罚范围<select name="scope">
          <option value="remaining" ${rulingDraft.scope === "remaining" ? "selected" : ""}>保留已完成局，剩余赛程判负</option>
          <option value="all" ${rulingDraft.scope === "all" ? "selected" : ""}>整场所有局判负</option>
        </select></label>
        <label class="adjudication-reason">判罚原因<input name="reason" maxlength="200" value="${esc(rulingDraft.reason)}" placeholder="例如：模型卡死且重连失败" required></label>
        <button class="danger" type="submit">确认判罚</button>
      </form>
    </details>` : "";
  const forcedDraft = forceFinishDraft(match);
  const forceScoreLimit = singleGames
    ? Number(match.game_limit || room.rules.game_count || 0) * 3
    : Number(match.bo2_limit || 0) * 3;
  const forceFinishControls = admin && room.status === "running"
    && acceptingResults && match.stage === "round_robin" ? `
    <details class="adjudication-controls" data-force-finish-details="${esc(match.id)}" ${forcedDraft.open ? "open" : ""}>
      <summary>强制结束并指定积分</summary>
      <form data-force-finish-form="${esc(match.id)}">
        <label>${esc(match.player_a_name)} 最终积分
          <input name="points_a" type="number" min="0" max="${forceScoreLimit}" step="1" value="${esc(forcedDraft.pointsA)}" required>
        </label>
        <label>${esc(match.player_b_name)} 最终积分
          <input name="points_b" type="number" min="0" max="${forceScoreLimit}" step="1" value="${esc(forcedDraft.pointsB)}" required>
        </label>
        <label class="adjudication-reason">结算原因
          <input name="reason" maxlength="200" value="${esc(forcedDraft.reason)}" placeholder="例如：选手弃权或无法继续比赛" required>
        </label>
        <p class="form-hint">填写本房间的最终总积分，范围 0-${forceScoreLimit}。已完成牌局和录像会保留，正在进行的未完成局不计入录像。</p>
        <button class="danger" type="submit">强制结束房间</button>
      </form>
    </details>` : "";
  const latestRuling = [...(match.administrative_rulings || [])]
    .reverse().find(ruling => ["forfeit", "forced_score"].includes(ruling.type));
  const adjudicationResult = latestRuling ? `
    <div class="adjudication-result">
      <div>
        <strong>${latestRuling.type === "forced_score"
          ? `管理员强制结算 · ${esc(latestRuling.points?.a)}:${esc(latestRuling.points?.b)}`
          : `管理员判罚 · ${esc(latestRuling.penalized_player_name)} 判负`}</strong>
        <span>${latestRuling.type === "forced_score"
          ? "房间最终积分"
          : `${latestRuling.scope === "all" ? "整场改判" : "剩余赛程判负"} · 影响 ${(latestRuling.affected_game_numbers || []).length} 局`}</span>
      </div>
      <p>${esc(latestRuling.reason)}</p>
      ${admin && match.can_undo_adjudication
        ? `<button class="link-btn" type="button" data-undo-adjudication="${esc(match.id)}">撤销本次判罚</button>`
        : ""}
    </div>` : "";
  const side = playerMode ? playerSide(match) : null;
  const session = match.session || { status: "waiting", player_ready: { a: false, b: false } };
  const showConnectionHealth = room.engine === "native-pvp" && acceptingResults;
  const aConnectionHealth = showConnectionHealth
    ? renderPlayerConnectionState(match, "a") : "";
  const bConnectionHealth = showConnectionHealth
    ? renderPlayerConnectionState(match, "b") : "";
  const continuous = (
    room.rules.auto_next_game
    && (
      room.room_type === "series"
      || (room.rules.parallel_matches && match.stage === "round_robin")
    )
  )
    || firstToFour;
  const nativeConnection = playerMode && acceptingResults && session.connection
    ? `<div class="native-connection">
        <strong>原生牌局连接</strong>
        <span>服务器 ${esc(session.connection.host)}:${esc(session.connection.port)} · 座位 ${esc(session.connection.seat)}</span>
        <code>${esc(session.connection.code)}</code>
        <div class="native-connection-actions">
          <button type="button" data-copy-value="${esc(session.connection.host)}:${esc(session.connection.port)}" data-copy-message="服务器地址已复制">复制地址</button>
          <button type="button" data-copy-value="${esc(session.connection.code)}" data-copy-message="连接码已复制">复制连接码</button>
        </div>
        <small>在 MDPro3 / YGOPro2 中输入上面的服务器、端口和连接码。</small>
        <small>双方可按任意顺序连接；若座位 1 先到，系统会自动等待座位 0 完成引擎握手。</small>
      </div>` : "";
  const myReady = side ? Boolean(session.player_ready?.[side]) : false;
  const playerPanel = playerMode && acceptingResults ? side
    ? continuous
      ? `<div class="player-ready-panel">
        <div><strong>你的席位：${side === "a" ? esc(match.player_a_name) : esc(match.player_b_name)}</strong>
          <span>自动续局已启用；使用固定密码进入当前牌局，结束后等待下一局建立。</span></div>
        <span class="pill">${esc(session.status || "waiting")}</span>
      </div>`
      : `<div class="player-ready-panel">
        <div><strong>你的席位：${side === "a" ? esc(match.player_a_name) : esc(match.player_b_name)}</strong>
          <span>${session.status === "ready" ? "双方已准备，等待原生引擎接入" : "进入牌局后点击准备"}</span></div>
        <button data-ready-match="${esc(match.id)}" class="${myReady ? "" : "primary"}" type="button">${myReady ? "取消准备" : "准备本局"}</button>
      </div>`
    : "" : "";
  const replayStatus = match.replay_status || {};
  const replayAvailable = match.status === "finished"
    && replayStatus.available === true
    && match.observer_connection?.status === "finished";
  const replayCount = Number(replayStatus.replay_count || 0);
  const expectedReplayCount = Number(
    replayStatus.expected_replay_count || match.total_game_count || 0
  );
  const missingGames = Array.isArray(replayStatus.missing_game_numbers)
    ? replayStatus.missing_game_numbers : [];
  const replayProgress = expectedReplayCount
    ? `${replayCount}/${expectedReplayCount}` : `${replayCount}`;
  const missingReplayText = missingGames.length
    ? `缺失第 ${missingGames.join("、")} 局` : "系列录像尚未完成归档";
  const replayDownload = match.status === "finished"
    && match.session?.pvp_session_id
    && replayCount > 0
    && (admin || (playerMode && side))
    ? `<div class="replay-download">
        <span>YRP 录像文件 · ${esc(replayProgress)}</span>
        <button type="button" data-replay-match="${esc(match.id)}">下载本房间录像</button>
      </div>`
    : "";
  const observer = match.observer_connection
    ? `<div class="observer-connection">
        <strong>${replayAvailable
          ? `${replayStatus.complete ? "系列赛录像" : "部分系列录像"} · ${esc(replayProgress)}`
          : "公开原生观战"}</strong>
        <span>${esc(match.observer_connection.host)}:${esc(match.observer_connection.port)}</span>
        <code>${esc(match.observer_connection.code)}</code>
        <button type="button" data-copy-value="${esc(match.observer_connection.host)}:${esc(match.observer_connection.port)}">复制地址</button>
        <button type="button" data-copy-value="${esc(match.observer_connection.code)}">${replayAvailable ? "复制录像密码" : "复制观战密码"}</button>
      </div>`
    : match.status === "finished" && room.engine === "native-pvp"
      ? `<div class="observer-connection replay-incomplete">
          <strong>录像不完整 · ${esc(replayProgress)}</strong>
          <span>${esc(missingReplayText)}</span>
        </div>`
    : room.engine === "native-pvp" && acceptingResults
      ? `<p class="form-hint">原生观战入口正在建立。</p>` : "";
  return `<div class="versus">
      <div class="side"><div class="side-player"><span>${esc(match.player_a_name)}</span>${aConnectionHealth}</div><strong>${aScore}</strong></div>
      <div class="versus-mark">VS</div>
      <div class="side right"><strong>${bScore}</strong><div class="side-player right"><span>${esc(match.player_b_name)}</span>${bConnectionHealth}</div></div>
    </div>
    <div class="match-progress">
      <span>${esc(phaseLabel(match))} · ${esc(bo2Text)}</span>
      <span>${esc(gameText)}</span>
      <span>${esc(seatLabel(match.next_seat_plan))}</span>
    </div>
    ${!isTiebreak ? `<p class="table-note">${firstToFour
      ? playAllSeven
        ? "真人决赛：双方固定同一副卡组，每局异种子，A/B/A/B 交替先手；固定打满 7 局，胜局多者夺冠，同分继续单局加赛。"
        : "真人决赛：双方固定同一副卡组，每局异种子，A/B/A/B 交替先手；一方先赢 4 局立即结束。"
      : singleGames
      ? "每局异种子、交替先手；每局胜者 3 分，平局各 1 分，打满即结束。"
      : "本对位 BO2 规则：两局换位，BO2 胜者 3 分，平局各 1 分。"}</p>` : ""}
    ${match.playoff_block_progress ? `<p class="callout">${esc(match.playoff_block_progress)}；同一组中一方双胜即结束，否则进入下一组。</p>` : ""}
    ${adjudicationResult}${observer}${replayDownload}${playerPanel}${nativeConnection}${controls}${adjudicationControls}${forceFinishControls}`;
}

function renderMatchRooms(room, admin, playerMode) {
  const matches = room.matches.filter(
    match => match.stage === "round_robin" || match.stage === "final"
  );
  return `<section class="match-rooms-section">
    <div class="section-head">
      <div><p class="eyebrow">DUEL ROOMS</p><h2>并行对局房间</h2></div>
      <span class="pill">${matches.filter(match => match.status === "finished").length} / ${matches.length} 已完成</span>
    </div>
    <div class="match-room-grid">${matches.map(match => `
      <article class="match-room-card ${esc(match.status)}">
        <div class="match-room-head">
          <span><strong>${esc(match.player_a_name)} vs ${esc(match.player_b_name)}</strong><code>${esc(match.duel_room_id)}</code></span>
          <span class="phase ${esc(match.phase)}">${match.status === "finished"
            ? esc(replayPhaseLabel(match)) : esc(phaseLabel(match))}</span>
        </div>
        ${renderActiveMatch(room, match, admin, playerMode)}
      </article>`).join("")}
    </div>
  </section>`;
}

function renderNoActive(room) {
  if (room.status === "lobby") return `<div class="empty-state"><strong>房间已创建</strong><span>等待管理员开始比赛。</span></div>`;
  if (room.status === "paused") return `<div class="empty-state"><strong>比赛已暂停</strong><span>当前比分已保存，恢复后继续。</span></div>`;
  if (room.status === "closed") return `<div class="empty-state"><strong>房间已关闭</strong><span>历史比分保留，不再接受对局。</span></div>`;
  const champion = room.players.find(player => player.id === room.champion)?.name
    || (room.final_opponent?.id === room.champion ? room.final_opponent.name : "");
  const qualifier = room.qualifying_champion?.name;
  return `<div class="empty-state"><strong>赛事已完成</strong><span>${champion ? `总冠军：${esc(champion)}` : "本场以平局结束"}${qualifier ? `；循环赛第一名：${esc(qualifier)}` : ""}。</span></div>`;
}

function renderSchedule(room) {
  return `<div class="schedule">${room.matches.map(match => {
    const running = ["running", "tiebreak"].includes(match.status);
    const result = match.status === "finished"
      ? match.winner
        ? `${match.winner === match.player_a ? match.player_a_name : match.player_b_name} 获胜`
        : "平局"
      : match.status === "closed" ? "已关闭"
      : match.status === "pending" ? room.status === "closed" ? "未开始" : "等待中" : phaseLabel(match);
    const score = match.phase === "tiebreak"
      ? `${match.playoff_block_wins.a}:${match.playoff_block_wins.b}`
      : `${match.points.a}:${match.points.b}`;
    return `<div class="schedule-row ${running ? "active" : ""}">
      <span class="match-number">${String(match.order).padStart(2, "0")}</span>
      <span class="schedule-names"><strong>${esc(match.player_a_name)}</strong><span>vs</span><strong>${esc(match.player_b_name)}</strong></span>
      <span class="schedule-score">${score}</span>
      <span class="schedule-result">${esc(result)}</span>
    </div>`;
  }).join("")}</div>`;
}

async function mutate(path) {
  try {
    state.room = await api(path, { method: "POST", body: "{}" });
    renderRoom();
    toast("已更新");
  } catch (error) {
    toast(error.message);
  }
}

async function closeRoom() {
  if (!window.confirm("确定关闭房间？正在进行的牌局将中断，关闭后无法恢复。")) return;
  try {
    state.room = await api(appPath(`/api/rooms/${state.room.id}/close`), {
      method: "POST", body: "{}",
    });
    state.nativeAccess = null;
    state.nativeSpectator = null;
    renderRoom();
    toast("房间已关闭");
  } catch (error) {
    toast(error.message);
  }
}

async function rotatePlayerAccess() {
  try {
    const room = await api(appPath(`/api/rooms/${state.room.id}/player-access`), {
      method: "POST", body: "{}",
    });
    state.room = room;
    state.playerAccess = room.player_access || null;
    state.finalOpponentAccess = room.final_opponent_access || null;
    renderRoom();
    toast("选手入口已重新生成");
  } catch (error) {
    toast(error.message);
  }
}

async function loadNativeSpectator(matchId = "") {
  try {
    const suffix = matchId ? `?match_id=${encodeURIComponent(matchId)}` : "";
    state.nativeSpectator = await api(appPath(
      `/api/rooms/${state.room.id}/native-spectator${suffix}`
    ));
    renderRoom();
  } catch (error) {
    toast(error.message);
  }
}

async function loadNativeAccess() {
  try {
    state.nativeAccess = await api(appPath(`/api/rooms/${state.room.id}/native-access`));
    renderRoom();
  } catch (error) {
    toast(error.message);
  }
}

async function playerReady(matchId) {
  if (!state.player || !state.room) return;
  const match = state.room.matches.find(item => item.id === matchId);
  if (!match) return;
  const side = playerSide(match);
  if (!side) return;
  const current = Boolean(match.session?.player_ready?.[side]);
  try {
    state.room = await api(appPath(
      `/api/rooms/${state.room.id}/matches/${match.id}/players/${state.player.id}/ready`
    ), {
      method: "POST", body: JSON.stringify({ ready: !current }),
    });
    renderRoom();
  } catch (error) {
    toast(error.message);
  }
}

async function recordGame(matchId, winner) {
  try {
    state.room = await api(appPath(`/api/rooms/${state.room.id}/matches/${matchId}/games`), {
      method: "POST", body: JSON.stringify({ winner }),
    });
    renderRoom();
  } catch (error) {
    toast(error.message);
  }
}

async function adjudicateMatch(form) {
  if (!state.room) return;
  const matchId = form.dataset.adjudicationForm;
  const match = state.room.matches.find(item => item.id === matchId);
  if (!match) return;
  const data = new FormData(form);
  const penalizedSide = data.get("penalized_side");
  const scope = data.get("scope");
  const reason = String(data.get("reason") || "").trim();
  const playerName = penalizedSide === "a"
    ? match.player_a_name : match.player_b_name;
  const scopeText = scope === "all"
    ? "覆盖已经完成的真实胜负，并将整场全部判负"
    : "保留已有结果，并将所有剩余局判负";
  if (!reason) {
    toast("请输入判罚原因");
    return;
  }
  if (!window.confirm(`确认将 ${playerName} 判负？\n${scopeText}。`)) return;
  try {
    state.room = await api(appPath(
      `/api/rooms/${state.room.id}/matches/${matchId}/adjudicate`
    ), {
      method: "POST",
      body: JSON.stringify({
        penalized_side: penalizedSide,
        scope,
        reason,
      }),
    });
    delete state.adjudicationDrafts[matchId];
    renderRoom();
    toast("判罚已结算，积分和排名已重新计算");
  } catch (error) {
    toast(error.message);
  }
}

async function forceFinishMatch(form) {
  if (!state.room) return;
  const matchId = form.dataset.forceFinishForm;
  const match = state.room.matches.find(item => item.id === matchId);
  if (!match) return;
  const data = new FormData(form);
  const pointsA = Number(data.get("points_a"));
  const pointsB = Number(data.get("points_b"));
  const reason = String(data.get("reason") || "").trim();
  if (
    !Number.isInteger(pointsA) || pointsA < 0
    || !Number.isInteger(pointsB) || pointsB < 0
  ) {
    toast("最终积分必须是非负整数");
    return;
  }
  if (!reason) {
    toast("请输入强制结算原因");
    return;
  }
  const message = [
    `确认强制结束 ${match.player_a_name} 对 ${match.player_b_name}？`,
    `最终积分：${pointsA}:${pointsB}`,
    "已完成牌局和录像会保留，当前未完成局会终止。",
  ].join("\n");
  if (!window.confirm(message)) return;
  try {
    state.room = await api(appPath(
      `/api/rooms/${state.room.id}/matches/${matchId}/force-finish`
    ), {
      method: "POST",
      body: JSON.stringify({
        points_a: pointsA,
        points_b: pointsB,
        reason,
      }),
    });
    delete state.forceFinishDrafts[matchId];
    renderRoom();
    toast("房间已强制结算，已完成录像正在封存");
  } catch (error) {
    toast(error.message);
  }
}

async function undoAdjudication(matchId) {
  if (!state.room) return;
  if (!window.confirm("确认撤销本次判罚并恢复判罚前的比赛状态？")) return;
  try {
    state.room = await api(appPath(
      `/api/rooms/${state.room.id}/matches/${matchId}/adjudication/undo`
    ), { method: "POST" });
    renderRoom();
    toast("判罚已撤销");
  } catch (error) {
    toast(error.message);
  }
}

function switchMode(mode) {
  if (mode === "admin" && state.adminRequired && !state.adminKey) {
    const key = window.prompt("请输入管理员密钥");
    if (!key) return;
    state.adminKey = key.trim();
    sessionStorage.setItem("tournament-admin-key", state.adminKey);
  }
  if (mode === "player" && !state.playerToken) {
    toast("请先使用选手入口令牌加入");
    return;
  }
  state.mode = mode;
  const params = new URLSearchParams({ room: state.room.id, mode });
  history.replaceState({}, "", `${mountPath}/?${params}`);
  renderRoom();
}

window.addEventListener("popstate", () => boot());
window.addEventListener("offline", () => setConnection(false));
window.addEventListener("online", () => {
  if (state.room) refreshRoom(state.room.id);
  else loadRooms();
});
$("#reload").addEventListener("click", () => {
  const roomId = new URLSearchParams(location.search).get("room");
  if (roomId) loadRoom(roomId);
  else renderHome();
});

async function boot() {
  try {
    const config = await fetch(appPath("/api/config")).then(async response => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
      return data;
    });
    state.adminRequired = Boolean(config.admin_required);
  } catch (_) {
    state.adminRequired = true;
  }
  const params = new URLSearchParams(location.search);
  const roomId = params.get("room");
  const token = params.get("token");
  if (token) {
    state.playerToken = token;
    state.mode = "player";
    sessionStorage.setItem("tournament-player-token", token);
    history.replaceState({}, "", `${mountPath}/?room=${encodeURIComponent(roomId || "")}&mode=player`);
  }
  if (roomId) loadRoom(roomId);
  else renderHome();
}

boot();

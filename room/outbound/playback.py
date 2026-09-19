"""Frame-by-frame playback of a recorded external duel.

The frames are built from the session's own network journal, replayed through the
same vendored shadow board the external client used, so a viewer sees exactly
what that client could observe: hidden opponent cards stay hidden. Each prompt is
paired with the response the model actually sent, which is resolved back to the
legal action it picked.

Card names are deliberately not resolved here; frames carry codes and a semantic
verb so the management page can translate them with the card data it already has.
"""
from itertools import islice
import json
from pathlib import Path
import struct

from .vendor.netduel import constants as C
from .vendor.netduel.actions import ActionAct, ActionPhase, SelectContext, parse_select
from .vendor.netduel.board import ShadowBoard
from .vendor.netduel.client import SELECT_MESSAGES

FRAME_LIMIT = 1500
MESSAGE_LIMIT = 60000

SESSION = None  # set by the manager to keep the id regex in one place

MOVE_EVENTS = {
    C.MSG_DRAW: ("draw", "抽卡"),
    C.MSG_NEW_TURN: ("turn", "新的回合"),
    C.MSG_NEW_PHASE: ("phase", "阶段变化"),
    C.MSG_SUMMONING: ("summon", "召唤"),
    C.MSG_SET: ("set", "盖放"),
    C.MSG_POS_CHANGE: ("position", "表示形式变化"),
    C.MSG_ATTACK: ("attack", "攻击宣言"),
    C.MSG_BATTLE: ("battle", "战斗"),
    C.MSG_CHAINING: ("chain", "效果发动"),
    C.MSG_CHAIN_END: ("chain_end", "连锁结束"),
    C.MSG_LPUPDATE: ("lp", "生命值变化"),
    C.MSG_DAMAGE: ("lp", "受到伤害"),
    C.MSG_RECOVER: ("lp", "回复生命值"),
    C.MSG_PAY_LPCOST: ("lp", "支付生命值"),
    C.MSG_WIN: ("win", "对局结束"),
}

ACT_VERB = {
    ActionAct.SET: "盖放魔法/陷阱",
    ActionAct.REPO: "变更表示形式",
    ActionAct.SPSUMMON: "特殊召唤",
    ActionAct.SUMMON: "召唤",
    ActionAct.MSET: "盖放怪兽",
    ActionAct.ATTACK: "攻击",
    ActionAct.DIRECT_ATTACK: "直接攻击",
    ActionAct.ACTIVATE: "发动效果",
    ActionAct.CANCEL: "放弃/取消",
    ActionAct.NONE: "选择",
}
PHASE_VERB = {ActionPhase.BATTLE: "进入战斗阶段", ActionPhase.MAIN2: "进入主要阶段 2",
              ActionPhase.END: "进入结束阶段", ActionPhase.NONE: "结束当前操作"}
PHASE_NAME = {1: "抽卡阶段", 2: "准备阶段", 4: "主要阶段 1", 8: "战斗阶段",
              0x10: "主要阶段 2", 0x20: "结束阶段"}

LOCATION_MZONE = 0x4
LOCATION_SZONE = 0x8
LOCATION_HAND = 0x1
LOCATION_GRAVE = 0x10
LOCATION_REMOVED = 0x20
LOCATION_DECK = 0x2
LOCATION_EXTRA = 0x40


def _zone(board, player, location):
    try:
        return board.zone(player, location)
    except Exception:
        return []


def _cards(entries):
    rows = []
    for card in entries or []:
        if card is None:
            rows.append(None)
            continue
        rows.append({"c": int(getattr(card, "code", 0) or 0),
                     "d": bool(getattr(card, "face_down", False)),
                     "a": int(getattr(card, "attack", 0) or 0),
                     "p": int(getattr(card, "position", 0) or 0)})
    return rows


def snapshot(board, state, event, text):
    return {
        "t": state["turn"], "ph": state["phase"], "lp": list(state["lp"]),
        "ev": event, "txt": text,
        "mon": {str(seat): _cards(_zone(board, seat, LOCATION_MZONE)) for seat in (0, 1)},
        "st": {str(seat): _cards(_zone(board, seat, LOCATION_SZONE)) for seat in (0, 1)},
        "cnt": {str(seat): {
            "hand": len(_zone(board, seat, LOCATION_HAND)),
            "grave": len(_zone(board, seat, LOCATION_GRAVE)),
            "banished": len(_zone(board, seat, LOCATION_REMOVED)),
            "deck": len(_zone(board, seat, LOCATION_DECK)),
            "extra": len(_zone(board, seat, LOCATION_EXTRA)),
        } for seat in (0, 1)},
    }


def _start_state(body):
    if len(body) >= 10:
        player, _pad, lp0, lp1 = struct.unpack_from("<BBii", body, 0)
        return {"turn": 1, "phase": 0, "lp": [int(lp0), int(lp1)]}
    return {"turn": 1, "phase": 0, "lp": [8000, 8000]}


def _update_state(state, msg, body):
    if msg == C.MSG_NEW_TURN:
        state["turn"] += 1
        state["phase"] = 0
    elif msg == C.MSG_NEW_PHASE and body:
        state["phase"] = body[0]
    elif msg == C.MSG_LPUPDATE and len(body) >= 5:
        player, value = struct.unpack_from("<Bi", body, 0)
        if player in (0, 1):
            state["lp"][player] = int(value)
    elif msg in (C.MSG_DAMAGE, C.MSG_RECOVER, C.MSG_PAY_LPCOST) and len(body) >= 5:
        player, amount = struct.unpack_from("<Bi", body, 0)
        if player in (0, 1):
            sign = -1 if msg != C.MSG_RECOVER else 1
            state["lp"][player] = max(0, state["lp"][player] + sign * int(amount))


def _chosen_action(prompt, response, model_seat):
    """Resolve the response back to the legal action it encodes, best effort."""
    msg, body = prompt
    try:
        result = parse_select(msg, body, SelectContext(max_options=128))
    except Exception as error:
        return {"actor": model_seat, "prompt": hex(msg), "label": "特殊选择",
                "detail": f"{type(error).__name__}", "index": None, "options": None}
    if result.auto_response is not None:
        return {"actor": result.player, "prompt": hex(msg), "label": "自动应答",
                "index": None, "options": None}
    selector = getattr(result, "selector", None)
    if selector is None:
        return {"actor": result.player, "prompt": hex(msg), "label": "选择", "index": None,
                "options": None}
    try:
        options = selector.options()
    except Exception:
        options = []
    index = None
    for position in range(min(len(options), 256)):
        try:
            if selector.choose(position) == response:
                index = position
                break
        except Exception:
            continue
    action = options[index] if index is not None and index < len(options) else None
    label, code = "选择", None
    if action is not None:
        code = int(getattr(action, "code", 0) or 0) or None
        if getattr(action, "act", ActionAct.NONE) != ActionAct.NONE:
            label = ACT_VERB.get(action.act, "选择")
        elif getattr(action, "phase", ActionPhase.NONE) != ActionPhase.NONE:
            label = PHASE_VERB.get(action.phase, "进入下一阶段")
        elif getattr(action, "spec", ""):
            label = "选择卡片位置"
            code = None
        else:
            label = getattr(action, "describe", lambda: "选择")()
    return {"actor": result.player, "prompt": hex(msg), "label": label, "code": code,
            "index": index, "options": len(options), "response": response.hex()}


def build(folder, model_seat):
    """Build the playback document for one recorded session."""
    journal = folder / "network.jsonl"
    if not journal.is_file():
        raise ValueError("This session has no network journal")
    deck_file = folder / "model.ydk"
    main, extra = [], []
    if deck_file.is_file():
        section = "main"
        for line in deck_file.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            text = line.strip()
            low = text.lower()
            if low in ("#main", "#extra", "!side", "#side"):
                section = "side" if low.startswith(("!", "#s")) else low[1:]
                continue
            if not text or text.startswith(("#", "!")):
                continue
            if section == "main":
                main.append(int(text))
            elif section == "extra":
                extra.append(int(text))
    board = ShadowBoard()
    board.start(int(model_seat), main, extra)
    state = {"turn": 0, "phase": 0, "lp": [8000, 8000]}
    frames, pending, applied, board_errors, total = [], None, 0, 0, 0

    with journal.open(encoding="utf-8", errors="replace") as stream:
        for line in islice(stream, MESSAGE_LIMIT):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            total += 1
            direction, opcode = event.get("direction"), event.get("opcode")
            payload = bytes.fromhex(event.get("payload") or "")
            if direction == "in" and opcode == 0x01 and payload:
                msg, body = payload[0], payload[1:]
                try:
                    board.apply(msg, body)
                    applied += 1
                except Exception:
                    board_errors += 1
                if msg == C.MSG_START:
                    state = _start_state(body)
                else:
                    _update_state(state, msg, body)
                if msg in SELECT_MESSAGES:
                    pending = (msg, body)
                elif msg in MOVE_EVENTS and len(frames) < FRAME_LIMIT:
                    kind, text = MOVE_EVENTS[msg]
                    frame = snapshot(board, state, kind, text)
                    if msg in (C.MSG_DAMAGE, C.MSG_RECOVER, C.MSG_PAY_LPCOST, C.MSG_LPUPDATE):
                        frame["txt"] = f"{text} → {'/'.join(map(str, state['lp']))}"
                    frames.append(frame)
            elif direction == "out" and opcode == 0x01 and pending is not None:
                if len(frames) < FRAME_LIMIT:
                    action = _chosen_action(pending, payload, model_seat)
                    frame = snapshot(board, state, "decision",
                                     PHASE_NAME.get(state["phase"], "行动"))
                    frame["act"] = action
                    frames.append(frame)
                pending = None
    return {"frames": frames, "meta": {"messages": total, "applied": applied,
                                       "board_errors": board_errors,
                                       "model_seat": int(model_seat), "truncated":
                                       len(frames) >= FRAME_LIMIT}}

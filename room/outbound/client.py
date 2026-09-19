"""Owner-started single-duel client; no automatic matchmaking or reconnect."""
from itertools import combinations
from contextlib import suppress
import hashlib
import lzma
import socket
import struct
import time

from .preference import prefer_first
from .vendor.netduel import constants as C, protocol as P
from .vendor.netduel.actions import MultiSelector, Reader, parse_select, ls_to_spec, SelectContext
from .vendor.netduel.client import NetDuelClient, DuelError, DuelResult, SELECT_MESSAGES
from .vendor.replay.actions import MultiSelect


def parse_prompt(msg, body, context):
    if msg != C.MSG_SELECT_TRIBUTE:
        return parse_select(msg, body, context)
    r = Reader(body)
    player, cancelable, minimum, maximum, count = (r.u8() for _ in range(5))
    specs, codes, weights, cards = [], [], [], []
    for _ in range(count):
        code, controller, location, sequence, weight = r.u32(), r.u8(), r.u8(), r.u8(), r.u8()
        specs.append(ls_to_spec(location, sequence, 0, controller != player))
        codes.append(code)
        weights.append(weight)
        cards.append((code, controller, location, sequence))
    if r.left:
        raise DuelError("Trailing bytes in tribute prompt")
    combos = [list(row) for size in range(0 if minimum == 0 else 1, min(count, maximum) + 1)
              for row in combinations(range(count), size)
              if sum(weights[i] for i in row) >= minimum]
    if not combos:
        raise DuelError("No legal tribute combination")
    from .vendor.netduel.actions import SelectResult
    selector = MultiSelector(msg, player, MultiSelect(minimum, maximum, 0, specs, 1, combos), codes)
    return SelectResult(msg, player, selector=selector, cards=cards)


def legacy_message(msg, body):
    if msg == C.MSG_SELECT_CHAIN:
        if len(body) < 12 or len(body) != 12 + body[1] * 13:
            raise DuelError("Invalid legacy chain prompt")
        converted = bytearray(body[:3] + body[4:12])
        for offset in range(12, len(body), 13):
            converted.extend(body[offset:offset + 1] + body[3:4] +
                             body[offset + 1:offset + 13])
        return bytes(converted)
    if msg == C.MSG_CONFIRM_CARDS:
        if len(body) < 2 or len(body) != 2 + body[1] * 7:
            raise DuelError("Invalid legacy card disclosure")
        return body[:1] + b"\0" + body[1:]
    return body


class RecordingStream(P.PacketStream):
    def __init__(self, sock, journal):
        super().__init__(sock)
        self.journal = journal

    def send(self, opcode, payload=b""):
        redacted = opcode in (P.CTOS.JOIN_GAME, P.CTOS.CHAT)
        self.journal("out", opcode, b"" if redacted else payload, redacted=redacted)
        super().send(opcode, payload)

    def recv(self, timeout=None):
        opcode, payload = super().recv(timeout)
        # Server chat can echo authentication commands.
        self.journal("in", opcode, b"" if opcode == P.STOC.CHAT else payload,
                     redacted=opcode == P.STOC.CHAT)
        return opcode, payload


class ExternalClient(NetDuelClient):
    def __init__(self, *args, password="", preference="auto", journal=None,
                 state_change=None, save_replay=None, deck_source="local",
                 auth_mode="none", account_password="", game_finished=None, **kwargs):
        super().__init__(*args, **kwargs)
        if deck_source not in ("local", "server_random") or auth_mode not in ("none", "duelchronicle"):
            raise ValueError("Invalid external deck/account mode")
        if auth_mode == "duelchronicle" and (deck_source != "server_random" or not account_password):
            raise ValueError("DuelChronicle requires server-assigned decks and account authentication")
        self.deck_source, self.auth_mode = deck_source, auth_mode
        self.account_password = account_password
        self.auth_sent = False
        self.authenticated = auth_mode == "none"
        self.auth_deadline = None
        self.submitted = False
        self.match_mode = False
        self.match_done = False
        self.match_kill = False
        self.games = []
        self.game_finished = game_finished or (lambda game: None)
        self.own_ready = False
        if deck_source == "server_random":
            from .server_deck import ServerDeckBoard
            self.main, self.extra, self.side = [], [], []
            self.board = ServerDeckBoard()
        self.go_first = (None if deck_source == "server_random" and preference == "auto"
                         else prefer_first(self.main, preference))
        self.password = password
        self.journal = journal or (lambda *a, **kw: None)
        self.state_change = state_change or (lambda *a, **kw: None)
        self.save_replay = save_replay or (lambda data: None)
        self.host_player = False
        self.replay_received = False
        self.replay_hashes = set()
        self.replay_errors = []
        self.initial_deck_counts = None
        self.terminal_seen = False
        self.terminal_time = None
        self.selector = None
        self.cancelled = False
        self.ctx.known_codes = list(dict.fromkeys(self.main + self.extra + self.side))

    def connect(self):
        self.state_change("connecting")
        sock = socket.create_connection((self.host, self.port), timeout=15)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.stream = RecordingStream(sock, self.journal)
        self.stream.send(P.CTOS.PLAYER_INFO, self.name.encode("utf-16-le").ljust(40, b"\0"))
        password = self.password
        self.password = ""
        # UTF-16 code units, not Python code points, define this protocol field.
        encoded = password.encode("utf-16-le")
        if len(encoded) > 38:
            raise DuelError("Room password is too long")
        self.stream.send(P.CTOS.JOIN_GAME,
                         struct.pack("<H2xI", self.version, 0) + encoded.ljust(40, b"\0"))

    def surrender(self):
        with suppress(Exception):
            super().surrender()

    def close(self):
        stream = self.stream
        try:
            with suppress(Exception):
                super().close()
        finally:
            if stream is not None:
                stream.close()
            self.stream = None

    def _handle(self, op, payload):
        if op == P.STOC.CHAT:
            if self.auth_mode == "duelchronicle" and not self.authenticated:
                text = P.decode_name(payload[2:])
                channel = int.from_bytes(payload[:2], "little")
                if self.auth_sent and channel >= 11 and text.startswith("[Server]:"):
                    if "\u767b\u5f55\u6210\u529f" in text:
                        self.authenticated = True
                        self.account_password = ""
                        self._submit_deck()
                    elif any(word in text for word in (
                            "\u5bc6\u7801\u9519\u8bef", "\u767b\u5f55\u5931\u8d25",
                            "\u5c1a\u672a\u6ce8\u518c", "\u672a\u6ce8\u518c")):
                        raise DuelError("account_login_failed")
            return
        if op == P.STOC.REPLAY:
            digest = hashlib.sha256(payload).hexdigest()
            if digest in self.replay_hashes:
                return
            try:
                self.save_replay(payload)
            except (ValueError, OSError, lzma.LZMAError) as error:
                # Recording is secondary to play. RecordingStream has already
                # journaled these original bytes durably, including rejected data.
                message = type(error).__name__ + ": " + str(error)[:250]
                if message not in self.replay_errors and len(self.replay_errors) < 10:
                    self.replay_errors.append(message)
                return
            self.replay_hashes.add(digest)
            self.replay_received = True
            return
        if op == P.STOC.TYPE_CHANGE and payload:
            self.host_player = bool(payload[0] & 0x10)
        if op == P.STOC.SELECT_TP:
            self.state_change("selecting_turn", preferred_first=self.go_first)
        if op == P.STOC.JOIN_GAME:
            self.state_change("joining")
            self.host_info = P.HostInfo.unpack(payload)
            self.match_mode = self.host_info.mode == 1 and self.auth_mode == "duelchronicle"
            if self.host_info.mode != 0 and not self.match_mode:
                raise DuelError(f"room mode {self.host_info.mode} is not a single duel")
            if self.authenticated:
                self._submit_deck()
            return
        if op == P.STOC.TYPE_CHANGE:
            self.lobby_pos = payload[0] & 0xF
            if self.lobby_pos == P.NETPLAYER_TYPE_OBSERVER:
                self.stream.send(P.CTOS.HS_TODUELIST)
                return
            if not self.authenticated:
                if not self.auth_sent:
                    self.state_change("authenticating")
                    self.stream.send(P.CTOS.CHAT, (
                        "/dl " + self.account_password + "\0").encode("utf-16-le"))
                    self.auth_sent = True
                    self.auth_deadline = time.monotonic() + 15
            elif self.auth_mode == "none":
                self.stream.send(P.CTOS.HS_READY)
                self.joined.set()
                self.state_change("waiting")
            return
        if op in (P.STOC.CHANGE_SIDE, P.STOC.WAITING_SIDE) and self.match_mode:
            if not self.terminal_seen:
                raise DuelError("Match side transition without a game result")
            # DuelChronicle replaces both decks and submits them itself.
            self.state_change("siding", games=len(self.games))
            return
        if op == P.STOC.ERROR_MSG:
            if len(payload) < 8:
                raise DuelError("Truncated server error")
            category, code = payload[0], int.from_bytes(payload[4:8], "little")
            names = {1: "join_rejected", 2: "deck_rejected", 3: "side_deck_rejected",
                     4: "protocol_version_mismatch"}
            raise DuelError(f"{names.get(category, 'server_error')}: {code}")
        if op == P.STOC.DUEL_END:
            # Some hosts deliver REPLAY immediately after DUEL_END.
            if self.terminal_seen:
                if self.match_mode:
                    self.match_done = True
                    self.terminal_time = time.monotonic()
                return
            raise DuelError("Server ended duel without an authoritative result")
        super()._handle(op, payload)
        if op == P.STOC.TYPE_CHANGE and self.lobby_pos in (0, 1):
            self.state_change("waiting")
        if op == P.STOC.HS_PLAYER_CHANGE and payload:
            if payload[0] >> 4 == self.lobby_pos:
                self.own_ready = payload[0] & 0xF == P.PLAYERCHANGE.READY
            if self.host_player and self.opponent_ready.is_set() and self.authenticated and (
                    self.auth_mode == "none" or self.own_ready):
                self.stream.send(P.CTOS.HS_START)

    def _submit_deck(self):
        if self.submitted:
            return
        self.stream.send(P.CTOS.UPDATE_DECK, P.update_deck(self.main + self.extra, self.side))
        self.submitted = True
        self.joined.set()
        self.state_change("waiting")

    def _game_msg(self, payload):
        if not payload:
            raise DuelError("Empty game message")
        msg, body = payload[0], payload[1:]
        if msg == C.MSG_START and self.games:
            if not self.match_mode or not self.terminal_seen or self.match_done:
                raise DuelError("Unexpected additional duel")
            if len(self.games) >= 5:
                raise DuelError("Match game limit exceeded")
            from .server_deck import ServerDeckBoard
            self.board = ServerDeckBoard()
            self.ctx = SelectContext(max_options=self.max_options, rng=self.rng, card_pool=self.card_pool)
            self.ctx.known_codes = []
            self.result = DuelResult()
            self.terminal_seen = self.replay_received = False
            self.selector = None
            self.terminal_time = None
        if self.version == 0x133E:
            body = legacy_message(msg, body)
        self._track(msg, body)
        self.board.apply(msg, body)
        self.policy.observe_game_message(msg, body)
        if msg == C.MSG_START:
            counts = struct.unpack_from("<4H", body, 10)
            self.initial_deck_counts = [list(counts[:2]), list(counts[2:])]
            self.state_change("dueling", model_seat=self.result.our_player)
        if msg == C.MSG_WIN:
            if self.terminal_seen:
                raise DuelError("Duplicate game result")
            self.terminal_seen = True
            self.terminal_time = None if self.match_mode else time.monotonic()
            self._duel_over = False
            self.result.lp = tuple(self._lp)
            game = {**self.result.as_dict(), "game": len(self.games) + 1,
                    "initial_deck_counts": self.initial_deck_counts}
            self.games.append(game)
            self.game_finished(game)
            self.state_change("saving", games=len(self.games))
        elif msg == C.MSG_MATCH_KILL:
            self.match_kill = True
        elif msg in SELECT_MESSAGES:
            self._answer(msg, body)

    def _answer(self, msg, body):
        result = parse_prompt(msg, body, self.ctx)
        if result.player != self.result.our_player:
            raise DuelError("Received another player's private prompt")
        if result.cards:
            problems = self.board.cross_check(result.cards)
            if problems:
                raise DuelError("Board/prompt disagreement: " + str(problems[0])[:200])
        if result.auto_response is not None:
            self.result.auto_responses += 1
            self._send_response(result.auto_response)
            return
        selector = self.selector = result.selector
        for _ in range(256):
            actions = selector.options()
            if not 1 <= len(actions) <= self.max_options:
                raise DuelError("Invalid legal-action count")
            state = self._decision_state(selector, actions)
            if len(actions) == 1:
                index, forced = 0, True
                self.result.forced_actions += 1
            else:
                index, forced = self.policy.choose(state), False
                if type(index) is not int or not 0 <= index < len(actions):
                    raise DuelError("Invalid model action")
                self.result.decisions += 1
                if self.result.decisions > 1000:
                    raise DuelError("Decision limit reached")
            action = actions[index]
            response = selector.choose(index)
            self.policy.record(state, action, selector, index, forced)
            if response is not None:
                self._send_response(response)
                return
        raise DuelError("Selection did not finish")

    def run(self):
        start = time.monotonic()
        try:
            self.connect()
            while not self.cancelled:
                if self.auth_deadline and not self.authenticated and time.monotonic() > self.auth_deadline:
                    raise DuelError("account_login_timeout")
                replays_complete = (len(self.replay_hashes) >= len(self.games)
                                    if self.match_mode else self.replay_received)
                if self.terminal_time is not None and (
                        replays_complete or time.monotonic() - self.terminal_time > 8):
                    break
                try:
                    op, payload = self.stream.recv(
                        1 if self.terminal_time is not None or not self.authenticated else self.timeout)
                except socket.timeout:
                    if self.terminal_time is not None or not self.authenticated:
                        continue
                    raise
                self._handle(op, payload)
        except (ConnectionError, OSError) as error:
            if not self.settled:
                self.result.error = type(error).__name__ + ": connection interrupted"
        except Exception as error:
            message = str(error)
            if self.account_password:
                message = message.replace(self.account_password, "[REDACTED]")
            self.result.error = type(error).__name__ + ": " + message[:300]
            self.surrender()
        finally:
            if self.cancelled and not self.settled:
                self.result.error = "stopped_by_owner"
            self.result.seconds = time.monotonic() - start
            self.result.lp = tuple(self._lp)
            self.close()
        return self.result

    @property
    def settled(self):
        return self.terminal_seen and (not self.match_mode or self.match_done)

    def outcome(self):
        if not self.settled or self.result.our_player not in (0, 1) or self.result.winner not in (0, 1, 2):
            return "incomplete"
        if not self.match_mode or self.match_kill:
            return "draw" if self.result.winner == 2 else "win" if self.result.won else "loss"
        wins = sum(game["winner"] == game["our_player"] for game in self.games)
        losses = sum(game["winner"] in (0, 1) and game["winner"] != game["our_player"] for game in self.games)
        return "win" if wins > losses else "loss" if losses > wins else "draw"

"""Server-assigned decks: known multiplicities and anonymous undisclosed cards."""
from collections import Counter
import struct

from .vendor.netduel import constants as C
from .vendor.netduel.board import ShadowBoard, ShadowCard, parse_query_segments


class ServerDeckBoard(ShadowBoard):
    def __init__(self):
        super().__init__()
        self.known_remaining = Counter()
        self.complete_deck = False

    def start(self, our_player, main, extra):
        # Never seed this view with the unrelated local selection.
        self.__init__()
        super().start(our_player, [], [])

    def _start_msg(self, body):
        super()._start_msg(body)
        player = self.our_player
        self.zones[player, C.LOCATION_EXTRA] = [
            ShadowCard(controller=player, owner=player, location=C.LOCATION_EXTRA,
                       sequence=i, position=C.POS_FACEDOWN_DEFENSE, hidden=True)
            for i in range(self.extra_count[player])]

    def _depart(self, code):
        code &= 0x7FFFFFFF
        if not code:
            self.known_remaining.clear()
            self.complete_deck = False
        elif self.known_remaining[code]:
            self.known_remaining[code] -= 1
        elif self.complete_deck:
            raise ValueError("Assigned deck departure disagrees with the disclosed deck")

    def _draw(self, body):
        if len(body) >= 2 and body[0] == self.our_player:
            if len(body) != 2 + 4 * body[1]:
                raise ValueError("Invalid assigned-deck draw")
            for offset in range(2, len(body), 4):
                self._depart(struct.unpack_from("<I", body, offset)[0])
        super()._draw(body)

    def _move(self, body):
        if len(body) >= 16:
            code, previous, current, _ = struct.unpack_from("<IIII", body)
            left = previous & 255 == self.our_player and (previous >> 8) & 255 == C.LOCATION_DECK
            entered = current & 255 == self.our_player and (current >> 8) & 255 == C.LOCATION_DECK
            if left and not entered:
                self._depart(code)
            elif entered and not left:
                if code & 0x7FFFFFFF:
                    self.known_remaining[code & 0x7FFFFFFF] += 1
                else:
                    self.complete_deck = False
        super()._move(body)

    def _update_data(self, body):
        if len(body) >= 2 and body[:2] == bytes((self.our_player, C.LOCATION_DECK)):
            segments = parse_query_segments(body[2:])
            if len(segments) != self.deck_count[self.our_player]:
                raise ValueError("Assigned deck refresh count mismatch")
            # Cache-only slots cannot establish identity in an unordered view.
            if any(segment and segment.get("_nochange") for segment in segments):
                self.complete_deck = False
                return
            codes = [segment.get("code", 0) & 0x7FFFFFFF if segment else 0 for segment in segments]
            self.known_remaining = Counter(code for code in codes if code)
            self.complete_deck = all(codes)
            self.updates += 1
            return
        super()._update_data(body)
        if len(body) >= 2 and body[:2] == bytes((self.our_player, C.LOCATION_EXTRA)):
            for i, card in enumerate(self.zone(self.our_player, C.LOCATION_EXTRA)):
                if card is not None:
                    card.controller, card.location, card.sequence = self.our_player, C.LOCATION_EXTRA, i

    def deck_codes(self, candidates):
        offered = {}
        for action in candidates:
            if action.spec.isdigit() and action.code:
                if action.spec in offered and offered[action.spec] != action.code:
                    raise ValueError("Conflicting assigned-deck candidate identity")
                offered[action.spec] = action.code
        visible = Counter(offered.values())
        if self.complete_deck and any(count > self.known_remaining[code] for code, count in visible.items()):
            raise ValueError("Candidate missing from disclosed assigned deck")
        remaining = self.known_remaining | visible
        count = self.deck_count[self.our_player]
        if sum(remaining.values()) > count:
            raise ValueError("Assigned deck candidates exceed remaining count")
        return list(remaining.elements()) + [0] * (count - sum(remaining.values()))

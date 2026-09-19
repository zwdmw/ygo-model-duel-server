import json
from pathlib import Path
import socket
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from .client import ExternalClient, RecordingStream, legacy_message, parse_prompt
from .preference import is_tenpai, prefer_first
from .run import REPLAY_LIMIT, replay_container
from .structured import CONTRACT, MESSAGES, StructuredEncoder, effect_id
from .vendor.netduel import constants as C, protocol as P
from .vendor.netduel.actions import (ActionAct, LegalAction, MultiSelector,
                                    SelectContext, Selector)
from .vendor.netduel.board import ShadowBoard
from .vendor.netduel.cards import CardData
from .vendor.netduel.policy import DecisionState
from .vendor.replay.actions import MultiSelect


PAIDRA, CHUNDRA = 39931513, 91810826

# STOC_REPLAY payload the YGO233 host returned for session 20260917-171959-7369:
# a YGOPro2 "yrp2" container, 727 bytes, stored by the client without rewriting.
HOST_REPLAY = (b"yrp2" + struct.pack("<8I", 0x1362, 0x11, 0, 0, 2065, 1789636833, 93, 1)
               + bytes(691))


def pool():
    cards = {PAIDRA: CardData(PAIDRA, type=33, level=3, attack=1700, defense=1000,
                              attribute=4, race=8192)}
    return SimpleNamespace(cards=cards, card_id=lambda code: 71 if code == PAIDRA else 0)


def state(actions, msg=C.MSG_SELECT_IDLECMD):
    board = ShadowBoard()
    board.start(0, [PAIDRA] * 40, [])
    board.apply(C.MSG_START, struct.pack("<BBiiHHHH", 0, 5, 8000, 8000, 40, 0, 40, 0))
    board.apply(C.MSG_DRAW, bytes((0, 1)) + struct.pack("<I", PAIDRA))
    return DecisionState(msg, 0, 0, actions, board, 1, C.PHASE_MAIN1, (8000, 8000))


class PreferenceTests(unittest.TestCase):
    def test_tenpai_uses_contents_not_filename(self):
        self.assertTrue(is_tenpai([PAIDRA] * 3 + [CHUNDRA]))
        self.assertFalse(prefer_first([PAIDRA] * 3 + [CHUNDRA]))
        self.assertTrue(prefer_first([123] * 40))
        self.assertFalse(is_tenpai([PAIDRA] * 3))

    def test_overrides(self):
        deck = [PAIDRA] * 3 + [CHUNDRA]
        self.assertTrue(prefer_first(deck, "first"))
        self.assertFalse(prefer_first([], "second"))
        self.assertIsNone(prefer_first(deck, "random"))
        with self.assertRaises(ValueError):
            prefer_first(deck, "bad")


class StructuredTests(unittest.TestCase):
    def test_native_contract_and_meaningful_structured_fields(self):
        encoder = StructuredEncoder(pool())
        encoder.reset(0)
        actions = [LegalAction(spec="h1", act=ActionAct.ACTIVATE, effect=0, code=PAIDRA),
                   LegalAction(phase=3)]
        view = state(actions)
        selector = Selector(view.msg, 0, actions, lambda *_: b"\0")
        obs = encoder.encode_state(view, selector)
        shapes = {"cards_": (1, 160, 41), "global_": (1, 23), "actions_": (1, 128, 12),
                  "h_actions_": (1, 32, 14), "action_ir_": (1, 128, 24),
                  "action_single_refs_": (1, 128, 4), "action_group_refs_": (1, 128, 5, 8),
                  "selection_": (1, 12), "public_events_": (1, 32, 16)}
        for key, shape in shapes.items():
            self.assertEqual(obs[key].shape, shape)
            self.assertEqual(obs[key].dtype, np.uint8)
        self.assertIsNone(obs["mask_"])
        self.assertEqual(int(obs["action_ir_"][0, 0, 1]), ActionAct.ACTIVATE)
        self.assertEqual(int(obs["action_ir_"][0, 0, 16]), 71)
        self.assertGreater(int(obs["action_single_refs_"][0, 0, 0]), 0)
        self.assertEqual(int(obs["action_ir_"][0, 1, 2]), 3)
        encoder.record(view, actions[0], selector, 0)
        obs = encoder.encode_state(view, selector)
        self.assertEqual(int(obs["public_events_"][0, 0, 0]), 1)
        self.assertEqual(int(obs["h_actions_"][0, 0, 3]), MESSAGES[view.msg])

    def test_multi_selection_context_refs_and_native_ids(self):
        encoder = StructuredEncoder(pool())
        ms = MultiSelect(1, 2, specs=["h1", "h2"])
        selector = MultiSelector(C.MSG_SELECT_CARD, 0, ms, [PAIDRA, PAIDRA])
        view = state(selector.options(), C.MSG_SELECT_CARD)
        view.board.apply(C.MSG_DRAW, bytes((0, 1)) + struct.pack("<I", PAIDRA))
        obs = encoder.encode_state(view, selector)
        self.assertEqual(obs["selection_"][0].tolist(), [3, 1, 0, 1, 2, 0, 0, 0, 0, 1, 0, 2])
        selector.choose(0)
        view.actions = selector.options()
        obs = encoder.encode_state(view, selector)
        self.assertEqual(int(obs["selection_"][0, 6]), 1)
        self.assertEqual(int(obs["action_group_mask_"][0, 0, 4, 0]), 1)
        self.assertGreater(int(obs["action_group_refs_"][0, 0, 4, 0]), 0)

    def test_race_uses_native_12_column_encoding(self):
        encoder = StructuredEncoder(pool())
        action = LegalAction(race=8192)
        row = np.zeros(12, dtype=np.uint8)
        encoder._write_action(row, C.MSG_ANNOUNCE_RACE, action, 0, 0)
        self.assertEqual(int(row[6]), 141)
        self.assertEqual(effect_id(0), 1)
        self.assertEqual(effect_id(10010), 2)
        self.assertEqual(effect_id(9999), 255)
        self.assertEqual(CONTRACT["source_sha256"],
                         "99942501ce4268f1fdd609b499788bbc2c9716469ea847eee863ef55f420388c")

    def test_overflow_fails_instead_of_truncating(self):
        encoder = StructuredEncoder(pool())
        with self.assertRaisesRegex(ValueError, "capacity"):
            encoder.encode_state(state([LegalAction()] * 129), None)

    def test_tribute_uses_native_combination_mode(self):
        body = bytes((0, 0, 2, 2, 3))
        for sequence, weight in enumerate((2, 1, 1)):
            body += struct.pack("<IBBBB", PAIDRA, 0, C.LOCATION_MZONE, sequence, weight)
        parsed = parse_prompt(C.MSG_SELECT_TRIBUTE, body, SelectContext(max_options=128))
        self.assertEqual(parsed.selector.ms.mode, 1)
        self.assertEqual(parsed.selector.ms.combs, [[0], [0, 1], [0, 2], [1, 2]])
        self.assertEqual(parsed.selector.choose(0), b"\x01\x00")

    def test_private_opponent_draw_does_not_expose_card_ids(self):
        encoder = StructuredEncoder(pool())
        view = state([LegalAction(phase=3), LegalAction(phase=1)])
        view.board.apply(C.MSG_DRAW, bytes((1, 1)) + struct.pack("<I", PAIDRA))
        selector = Selector(view.msg, 0, view.actions, lambda *_: b"\0")
        obs = encoder.encode_state(view, selector)
        cards = obs["cards_"][0]
        hidden = (cards[:, 4] == 1) & np.isin(cards[:, 2], [1, 2, 7])
        self.assertTrue((cards[hidden, :2] == 0).all())


class ReplayContainerTests(unittest.TestCase):
    """The host's own container is accepted verbatim; nothing is reinterpreted."""

    def test_ygo_pro2_host_replay_is_accepted(self):
        self.assertEqual(len(HOST_REPLAY), 727)
        self.assertEqual(replay_container(HOST_REPLAY), "yrp2")
        self.assertEqual(replay_container(b"yrp1" + bytes(124)), "yrp1")

    def test_unknown_or_out_of_bounds_containers_are_refused(self):
        for raw in (b"yrp0" + bytes(124), b"yrp3" + bytes(124), b"", b"yrp2",
                    b"yrp2" + bytes(27), b"yrp2" + bytes(REPLAY_LIMIT - 3)):
            with self.assertRaisesRegex(ValueError, "Unsupported external replay format"):
                replay_container(raw)

    def test_client_stores_the_host_payload_unchanged(self):
        received = []
        client = ExternalClient("127.0.0.1", 1, "TestBot", [PAIDRA] * 40, [],
                                SimpleNamespace(), save_replay=received.append)
        client._handle(P.STOC.REPLAY, HOST_REPLAY)
        self.assertEqual(received, [HOST_REPLAY])
        self.assertEqual(replay_container(received[0]), "yrp2")
        self.assertTrue(client.replay_received)


class NetworkTests(unittest.TestCase):
    def test_explicit_legacy_chain_conversion(self):
        body = bytes.fromhex("0002010000808000008000000038fa68010002000a80a38f1600cf38fa030002010a00000000")
        parsed = parse_prompt(C.MSG_SELECT_CHAIN, legacy_message(C.MSG_SELECT_CHAIN, body),
                              SelectContext())
        self.assertEqual(len(parsed.selector.options()), 3)
        self.assertEqual(parsed.selector.options()[-1].act, ActionAct.CANCEL)
        self.assertEqual(parsed.selector.choose(2), b"\xff" * 4)

    def test_win_rps_selects_second_and_waits_for_replay(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        observed, journal, replays, failures = [], [], [], []
        password = "private-room"

        def host():
            try:
                sock, _ = listener.accept()
                stream = P.PacketStream(sock)
                observed.append(stream.recv(3))
                observed.append(stream.recv(3))
                stream.send(P.STOC.SELECT_TP)
                observed.append(stream.recv(3))
                start = struct.pack("<BBiiHHHH", 1, 5, 8000, 8000, 40, 0, 40, 0)
                stream.send(P.STOC.GAME_MSG, bytes((C.MSG_START,)) + start)
                stream.send(P.STOC.GAME_MSG, bytes((C.MSG_WIN, 1, 1)))
                stream.send(P.STOC.DUEL_END)
                stream.send(P.STOC.REPLAY, HOST_REPLAY)
                stream.recv(3)
                stream.close()
            except Exception as error:
                failures.append(error)
            finally:
                listener.close()

        thread = threading.Thread(target=host)
        thread.start()
        policy = SimpleNamespace(bind=lambda client: None, observe_game_message=lambda *args: None)
        client = ExternalClient("127.0.0.1", port, "TestBot", [PAIDRA] * 3 + [CHUNDRA] * 37,
                                [], policy, password=password,
                                journal=lambda *args, **kwargs: journal.append((args, kwargs)),
                                save_replay=replays.append)
        result = client.run()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(result.went_first)
        self.assertTrue(result.won)
        self.assertEqual(observed[2], (P.CTOS.TP_RESULT, b"\0"))
        self.assertEqual(observed[1][1][8:].decode("utf-16-le").rstrip("\0"), password)
        joins = [(args, flags) for args, flags in journal
                 if args[:2] == ("out", P.CTOS.JOIN_GAME)]
        self.assertEqual(joins, [(("out", P.CTOS.JOIN_GAME, b""), {"redacted": True})])
        self.assertEqual(replays, [HOST_REPLAY])
        self.assertTrue(client.terminal_seen)

    def test_bad_action_is_not_replaced_with_a_heuristic(self):
        client = ExternalClient("127.0.0.1", 1, "Test", [PAIDRA] * 40, [],
                                SimpleNamespace(choose=lambda state: 999))
        client.result.our_player = 0
        with self.assertRaisesRegex(Exception, "Invalid model action"):
            client._answer(C.MSG_SELECT_YESNO, bytes((0,)) + struct.pack("<I", 30))


if __name__ == "__main__":
    unittest.main()

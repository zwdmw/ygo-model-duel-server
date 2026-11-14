import json
from pathlib import Path
import shutil
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required to test the Evolution message view")
class SpectatorViewTest(unittest.TestCase):
    def test_reveals_hands_but_keeps_unknown_deck_cards_hidden(self):
        helper = SOURCE / "deploy/evolution-patch/SpectatorView.js"
        script = r"""
const assert = require("node:assert/strict");
const { spectatorView } = require(HELPER);

function message(identifier, properties = {}) {
  const value = {
    constructor: { identifier },
    observerCalls: 0,
    ...properties,
    copy() {
      const copy = { ...this };
      copy.copy = this.copy;
      copy.observerView = this.observerView;
      return copy;
    },
    observerView() {
      this.observerCalls += 1;
      const copy = this.copy();
      if (this.constructor.identifier === 31) return copy;
      if (Array.isArray(copy.cards)) {
        copy.cards = copy.cards.map((card) => {
          if (typeof card === "number") return 0;
          const location = Number(card.location) || 0;
          return location & (1 | 0x40)
            ? { ...card, code: (Number(card.code) >>> 31) ? card.code : 0 }
          : { ...card, code: 0 };
        });
      }
      if (copy.location & (1 | 0x40)) {
        if (copy.card) {
          copy.card = {
            ...copy.card,
            code: (Number(copy.card.code) >>> 31) ? copy.card.code : 0,
          };
        }
        if (typeof copy.code === "number") {
          copy.code = (Number(copy.code) >>> 31) ? copy.code : 0;
        }
      } else if (typeof copy.code === "number") {
        copy.code = 0;
      }
      if (typeof copy.topCode === "number") {
        copy.topCode = (Number(copy.topCode) >>> 31) ? copy.topCode : 0;
      }
      if (Array.isArray(copy.handCards)) copy.handCards = copy.handCards.map(() => 0);
      if (Array.isArray(copy.extraCards)) {
        copy.extraCards = copy.extraCards.map((code) =>
          (Number(code) >>> 31) ? code : 0);
      }
      return copy;
    },
  };
  return value;
}

const draw = message(90, { cards: [1234, 5678] });
assert.equal(spectatorView(draw, true), draw);
assert.deepEqual(draw.cards, [1234, 5678]);

const shuffleHand = message(33, { cards: [1234, 5678] });
assert.equal(spectatorView(shuffleHand, true), shuffleHand);

const privateTop = message(38, { code: 1234 });
assert.equal(spectatorView(privateTop, true).code, 0);
const publicTop = message(38, { code: 0x80001234 });
assert.equal(spectatorView(publicTop, true).code, 0x80001234);

const confirmed = message(31, {
  cards: [
    { code: 1234, location: 1 },
    { code: 5678, location: 2 },
    { code: 9012, location: 4 },
    { code: 7890, location: 0x40 },
    { code: 0x80003456, location: 1 },
  ],
});
assert.deepEqual(
  spectatorView(confirmed, true).cards.map((card) => card.code),
  [0, 5678, 9012, 0, 0x80003456],
);
assert.deepEqual(
  confirmed.cards.map((card) => card.code),
  [1234, 5678, 9012, 7890, 0x80003456],
);

const deckTopConfirmation = message(30, {
  cards: [
    { code: 1234, location: 1 },
    { code: 0x80005678, location: 1 },
  ],
});
assert.deepEqual(
  spectatorView(deckTopConfirmation, true).cards.map((card) => card.code),
  [0, 0x80005678],
);

const shuffledExtra = message(39, { cards: [1234, 0x80005678] });
assert.deepEqual(spectatorView(shuffledExtra, true).cards, [0, 0x80005678]);

const handUpdate = message(6, { location: 2, cards: [{ code: 5678 }] });
assert.equal(spectatorView(handUpdate, true), handUpdate);
const handCardUpdate = message(7, { location: 2, card: { code: 5678 } });
assert.equal(spectatorView(handCardUpdate, true), handCardUpdate);
const deckUpdate = message(6, { location: 1, cards: [{ code: 1234 }] });
assert.equal(spectatorView(deckUpdate, true).cards[0].code, 0);
assert.equal(deckUpdate.observerCalls, 1);
const deckCardUpdate = message(7, { location: 1, card: { code: 1234 } });
assert.equal(spectatorView(deckCardUpdate, true).card.code, 0);
const publiclyRevealedDeckCard = message(7, {
  location: 1,
  card: { code: 0x80001234 },
});
assert.equal(spectatorView(publiclyRevealedDeckCard, true).card.code, 0x80001234);

const tagSwap = message(161, {
  handCards: [5678],
  topCode: 1234,
  extraCards: [5678, 0x80001234],
});
const tagView = spectatorView(tagSwap, true);
assert.deepEqual(tagView.handCards, [5678]);
assert.equal(tagView.topCode, 0);
assert.deepEqual(tagView.extraCards, [0, 0x80001234]);
const publicTagSwap = message(161, {
  handCards: [0x80005678],
  topCode: 0x80001234,
  extraCards: [0x80009012],
});
assert.equal(spectatorView(publicTagSwap, true).topCode, 0x80001234);

const ordinary = message(90, { cards: [1234] });
assert.equal(spectatorView(ordinary, false).cards[0], 0);
const unclassified = message(181, { code: 1234 });
assert.equal(spectatorView(unclassified, true).code, 0);
assert.equal(unclassified.observerCalls, 1);
"""
        script = script.replace("HELPER", json.dumps(str(helper)))
        result = subprocess.run(
            [NODE, "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

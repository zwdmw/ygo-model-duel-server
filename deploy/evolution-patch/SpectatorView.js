"use strict";

const LOCATION_HAND = 0x02;
const LOCATION_DECK = 0x01;
const LOCATION_EXTRA = 0x40;

const MSG_UPDATE_DATA = 6;
const MSG_UPDATE_CARD = 7;
const MSG_CONFIRM_CARDS = 31;
const MSG_DECK_TOP = 38;
const MSG_CONFIRM_DECKTOP = 30;
const MSG_CONFIRM_EXTRATOP = 42;
const MSG_SHUFFLE_EXTRA = 39;
const MSG_DRAW = 90;
const MSG_SHUFFLE_HAND = 33;
const MSG_TAG_SWAP = 161;

function isPublicCode(code) {
    return ((Number(code) >>> 0) & 0x80000000) !== 0;
}

function spectatorView(message, fullSpectator) {
    if (!fullSpectator) {
        return message.observerView();
    }

    const identifier = message.constructor.identifier;
    if (identifier === MSG_DRAW || identifier === MSG_SHUFFLE_HAND) {
        return message;
    }
    if ((identifier === MSG_UPDATE_DATA || identifier === MSG_UPDATE_CARD) &&
        (message.location & LOCATION_HAND)) {
        return message;
    }
    if (identifier === MSG_DECK_TOP) {
        const view = message.copy();
        if (!isPublicCode(view.code)) {
            view.code = 0;
        }
        return view;
    }
    if (identifier === MSG_CONFIRM_DECKTOP ||
        identifier === MSG_CONFIRM_EXTRATOP ||
        identifier === MSG_SHUFFLE_EXTRA) {
        const view = message.copy();
        view.cards = view.cards.map((card) => {
            if (typeof card === "number") {
                return isPublicCode(card) ? card : 0;
            }
            return {
                ...card,
                code: isPublicCode(card.code) ? card.code : 0,
            };
        });
        return view;
    }
    if (identifier === MSG_CONFIRM_CARDS) {
        const view = message.copy();
        view.cards = view.cards.map((card) => (
            card.location & (LOCATION_DECK | LOCATION_EXTRA)
                ? { ...card, code: isPublicCode(card.code) ? card.code : 0 }
                : card
        ));
        return view;
    }
    if (identifier === MSG_TAG_SWAP) {
        const view = message.observerView();
        view.handCards = [...message.handCards];
        if (!isPublicCode(view.topCode)) {
            view.topCode = 0;
        }
        return view;
    }
    return message.observerView();
}

module.exports = { spectatorView };

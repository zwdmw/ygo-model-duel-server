"""Convert an external (YGO233) `yrp2` replay into a plain ygopro `yrp1` replay.

YGO233 stores replays in an extended container that the native clients cannot
open: the header is 80 bytes (32-byte base + seed_sequence[8] + header_version +
3 x u32) and the body is raw LZMA1 (no end-of-payload marker). MDPro3/YGOPro2
read a 32-byte header and treat everything after it as the body, so they try to
LZMA-decode the extended header bytes, throw, and silently fall back to the main
menu (`MDPro3.SelectReplay.GetYRP` in the player log).

Conversion keeps the body verbatim and rewrites the container:

    base header (32 B)
        0..3    id            'yrp2' -> 'yrp1'
        4..7    version       kept
        8..11   flag          compressed bit cleared, other bits kept
        12..15  seed          seed_sequence[0] when non-zero, else header seed
        16..19  datasize      len(plaintext)
        20..23  start_time    kept
        24..31  props         zeroed (the body is not compressed)
        32..    plaintext body
"""
from __future__ import annotations

import lzma
import struct

BASE_HEADER = 32
EXT_HEADER = 80
COMPRESSED = 0x1


class ConversionError(ValueError):
    """The replay is not an extended container we know how to rewrite."""


def _lzma_params(props):
    packed = props[0]
    return (packed % 9, (packed // 9) % 5, packed // 45,
            struct.unpack_from("<I", props, 1)[0] or (1 << 24))


def _decompress(body, props, datasize):
    lc, lp, pb, dict_size = _lzma_params(props)
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
        "id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}])
    try:
        plain = decoder.decompress(body)
    except lzma.LZMAError:
        plain = b""                      # expected: the stream has no end marker
    if len(plain) < datasize:
        # Fall back to .lzma framing, which stops once datasize bytes are known.
        framed = bytes([props[0]]) + struct.pack("<I", dict_size) + struct.pack("<Q", datasize) + body
        try:
            plain = lzma.decompress(framed, format=lzma.FORMAT_ALONE)
        except lzma.LZMAError:
            pass
    return plain


def parse_body(plain):
    """Structural self-check: names, duel parameters, decks and the response stream."""
    if len(plain) < 96:
        raise ConversionError("replay body is too short")
    info = {"names": [plain[i * 40:(i + 1) * 40].decode("utf-16-le", "replace").split("\x00")[0]
                      for i in range(2)]}
    start_lp, start_hand, draw_count, opt = struct.unpack_from("<4i", plain, 80)
    info.update(start_lp=start_lp, start_hand=start_hand, draw_count=draw_count,
                master_rule=opt >> 16)
    position = 96
    decks = []
    for _ in range(2):
        main_count, = struct.unpack_from("<i", plain, position)
        position += 4 + 4 * main_count
        extra_count, = struct.unpack_from("<i", plain, position)
        position += 4 + 4 * extra_count
        decks.append((main_count, extra_count))
    info["decks"] = decks
    records = position
    while records < len(plain):
        length = plain[records]
        records += 1 + length
        if records > len(plain):
            raise ConversionError("replay response stream is truncated")
    info["records_end"] = records
    return info


def to_playable(raw):
    """Return a plain yrp1 container whose body is the decompressed plaintext."""
    if raw[:4] == b"yrp1":
        return raw
    if raw[:4] != b"yrp2":
        raise ConversionError("not a YRP replay: magic=%r" % (raw[:4],))
    if len(raw) <= EXT_HEADER:
        raise ConversionError("replay is too short for an extended header")
    version, flag, seed = struct.unpack_from("<III", raw, 4)
    datasize, start_time = struct.unpack_from("<Ii", raw, 16)
    props = raw[24:32]
    seed_sequence = struct.unpack_from("<8I", raw, 32)
    if flag & COMPRESSED:
        plain = _decompress(raw[EXT_HEADER:], props, datasize)
    else:
        plain = raw[BASE_HEADER:]
    if not plain:
        raise ConversionError("could not decompress the replay body")
    parse_body(plain)
    chosen_seed = seed_sequence[0] or seed
    header = struct.pack("<4sIIIIi", b"yrp1", version, flag & ~COMPRESSED,
                         chosen_seed, len(plain), start_time) + bytes(8)
    return header + plain


def describe(raw):
    """Human-readable summary for logs (never raises on odd input)."""
    try:
        playable = to_playable(raw)
        info = parse_body(playable[BASE_HEADER:])
    except (ConversionError, struct.error) as error:
        return "replay conversion failed: %s" % error
    return ("replay converted: %s vs %s, rule %s, decks %s, %d body bytes, seed %d"
            % (info["names"][0] or "?", info["names"][1] or "?",
               info["master_rule"], info["decks"], len(playable) - BASE_HEADER,
               struct.unpack_from("<I", playable, 12)[0]))

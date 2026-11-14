"""Strict fixed-deck loading for native YGOPro sessions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from . import protocol as P


SECTION_NAMES = {
    "#main": "main",
    "#extra": "extra",
    "!side": "side",
}


@dataclass(frozen=True)
class FixedDeck:
    path: Path
    name: str
    main: tuple[int, ...]
    extra: tuple[int, ...]
    side: tuple[int, ...]
    source_sha256: str
    canonical_sha256: str
    payload: bytes
    payload_sha256: str

    def public_metadata(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source_sha256": self.source_sha256,
            "canonical_sha256": self.canonical_sha256,
            "payload_sha256": self.payload_sha256,
            "counts": {
                "main": len(self.main),
                "extra": len(self.extra),
                "side": len(self.side),
            },
        }


def _canonical_bytes(
    main: tuple[int, ...],
    extra: tuple[int, ...],
    side: tuple[int, ...],
) -> bytes:
    return (
        f"main:{','.join(map(str, main))}\n"
        f"extra:{','.join(map(str, extra))}\n"
        f"side:{','.join(map(str, side))}"
    ).encode("ascii")


def load_ydk(path: Path) -> FixedDeck:
    path = Path(path)
    try:
        source = path.read_bytes()
    except OSError as error:
        raise ValueError(f"fixed deck cannot be read: {path}") from error
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("fixed deck must be UTF-8 or ASCII") from error

    sections: dict[str, list[int]] = {"main": [], "extra": [], "side": []}
    seen: set[str] = set()
    current: str | None = None
    for line_number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if not value:
            continue
        section = SECTION_NAMES.get(value.lower())
        if section is not None:
            if section in seen:
                raise ValueError(f"fixed deck repeats {value} at line {line_number}")
            seen.add(section)
            current = section
            continue
        if value.startswith("#"):
            continue
        if current is None:
            raise ValueError(
                f"fixed deck card appears before a section at line {line_number}"
            )
        if not value.isascii() or not value.isdecimal():
            raise ValueError(f"fixed deck has an invalid card code at line {line_number}")
        code = int(value)
        if not 0 < code <= 0xFFFFFFFF:
            raise ValueError(f"fixed deck card code is out of range at line {line_number}")
        sections[current].append(code)

    missing = set(sections) - seen
    if missing:
        raise ValueError(
            "fixed deck is missing sections: " + ", ".join(sorted(missing))
        )
    main = tuple(sections["main"])
    extra = tuple(sections["extra"])
    side = tuple(sections["side"])
    payload = P.deck_payload(list(main), list(extra), list(side))
    return FixedDeck(
        path=path.resolve(),
        name=path.stem,
        main=main,
        extra=extra,
        side=side,
        source_sha256=hashlib.sha256(source).hexdigest(),
        canonical_sha256=hashlib.sha256(
            _canonical_bytes(main, extra, side)
        ).hexdigest(),
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
    )

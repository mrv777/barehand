"""Hermes key names / text -> QEMU ``QKeyCode`` names (US keyboard layout). Pure functions, no I/O."""

from __future__ import annotations

import re
from typing import List

MAX_SEQUENCE = 64

_PLAIN = {
    " ": "spc", "\n": "ret", "\t": "tab", "-": "minus", "=": "equal", "[": "bracket_left",
    "]": "bracket_right", "\\": "backslash", ";": "semicolon", "'": "apostrophe",
    "`": "grave_accent", ",": "comma", ".": "dot", "/": "slash",
}
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
    ")": "0", "_": "minus", "+": "equal", "{": "bracket_left", "}": "bracket_right",
    "|": "backslash", ":": "semicolon", '"': "apostrophe", "~": "grave_accent", "<": "comma",
    ">": "dot", "?": "slash",
}

# Friendly names (what a model writes) -> qcode. Lower-cased before lookup.
_NAMED = {
    "return": "ret", "enter": "ret", "ret": "ret", "escape": "esc", "esc": "esc", "tab": "tab",
    "space": "spc", "spc": "spc", "spacebar": "spc", "backspace": "backspace", "bksp": "backspace",
    "delete": "delete", "del": "delete", "forwarddelete": "delete",
    "insert": "insert", "ins": "insert",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "home": "home", "end": "end", "pageup": "pgup", "page_up": "pgup", "pgup": "pgup",
    "pagedown": "pgdn", "page_down": "pgdn", "pgdn": "pgdn",
    "capslock": "caps_lock", "caps_lock": "caps_lock", "caps": "caps_lock",
    "numlock": "num_lock", "num_lock": "num_lock", "scrolllock": "scroll_lock",
    "scroll_lock": "scroll_lock", "print": "print", "printscreen": "print", "prtsc": "print",
    "sysrq": "sysrq", "pause": "pause", "break": "pause", "menu": "menu", "apps": "menu",
    "ctrl": "ctrl", "control": "ctrl", "lctrl": "ctrl", "rctrl": "ctrl_r",
    "alt": "alt", "option": "alt", "opt": "alt", "lalt": "alt", "ralt": "alt_r", "altgr": "alt_r",
    "shift": "shift", "lshift": "shift", "rshift": "shift_r",
    "cmd": "meta_l", "command": "meta_l", "win": "meta_l", "windows": "meta_l", "super": "meta_l",
    "meta": "meta_l",
    # numeric keypad
    "kp_enter": "kp_enter", "kp_add": "kp_add", "kp_subtract": "kp_subtract",
    "kp_multiply": "kp_multiply", "kp_divide": "kp_divide", "kp_decimal": "kp_decimal",
    **{f"kp_{i}": f"kp_{i}" for i in range(10)},
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}
# Names for single punctuation keys, usable inside a combo ("ctrl+minus", "alt+comma").
_PUNCT_NAMES = {
    "minus": "minus", "dash": "minus", "equal": "equal", "equals": "equal", "plus": "plus",
    "comma": "comma", "period": "dot", "dot": "dot", "slash": "slash", "backslash": "backslash",
    "semicolon": "semicolon", "quote": "apostrophe", "apostrophe": "apostrophe",
    "grave": "grave_accent", "backtick": "grave_accent", "bracketleft": "bracket_left",
    "bracketright": "bracket_right",
}

# Every qcode this module can emit. tests/test_integration.py checks them against the QKeyCode enum of the
# running QEMU (QMP.valid_qcodes); the unit tests check them against QEMU 11's list captured in tests.
ALL_QCODES = frozenset(
    set(_NAMED.values()) | set(_PLAIN.values()) | set(_SHIFTED.values()) | set(_PUNCT_NAMES.values())
    - {"plus"} | {"shift"} | set("abcdefghijklmnopqrstuvwxyz0123456789"))


class KeyError_(ValueError):
    """Unmappable key or character; the message is shown to the model."""


def char_to_qcodes(ch: str) -> List[str]:
    """One typed character -> qcodes pressed together (shift first)."""
    if len(ch) != 1:
        raise KeyError_(f"expected a single character, got {ch!r}")
    if ch.isascii() and ch.isalpha():
        return (["shift"] if ch.isupper() else []) + [ch.lower()]
    if ch.isascii() and ch.isdigit():
        return [ch]
    if ch in _PLAIN:
        return [_PLAIN[ch]]
    if ch in _SHIFTED:
        return ["shift", _SHIFTED[ch]]
    shown = ch if ch.isprintable() else f"U+{ord(ch):04X}"
    raise KeyError_(f"cannot type {shown!r}: only US-keyboard ASCII can be typed (non-ASCII has no key mapping)")


def text_to_chords(text: str) -> List[List[str]]:
    """Validate and convert *all* of ``text`` up front, so nothing is typed if any character is bad.
    ``\\r\\n`` and ``\\r`` count as one Enter."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [char_to_qcodes(ch) for ch in text]


def _token_to_qcodes(token: str, original: str) -> List[str]:
    low = token.lower()
    if low in _NAMED:
        return [_NAMED[low]]
    if low in _PUNCT_NAMES:
        name = _PUNCT_NAMES[low]
        return ["shift", "equal"] if name == "plus" else [name]
    if len(token) == 1:
        return char_to_qcodes(token)
    raise KeyError_(
        f"unknown key {token!r} in {original!r}. Use names like return, escape, tab, space, backspace, "
        "delete, insert, up/down/left/right, home, end, pageup, pagedown, f1-f12, ctrl, alt, shift, "
        "cmd/win, or single characters (ctrl+c, alt+f2, shift+tab).")


def combo_to_qcodes(combo: str) -> List[str]:
    """``'ctrl+alt+delete'`` -> ``['ctrl', 'alt', 'delete']`` (one chord, pressed together). Whitespace around
    ``+`` is ignored (``'ctrl + c'``) and hyphenated combos work (``'ctrl-alt-del'``)."""
    combo = combo.strip()
    if not combo:
        raise KeyError_("empty key combo")
    if combo == "+":
        return _token_to_qcodes("+", combo)
    if combo.endswith("++"):  # "ctrl++" -> ctrl and the plus key
        parts = [p.strip() for p in combo[:-2].split("+")] + ["+"]
    else:
        parts = [p.strip() for p in combo.split("+")]
    out: List[str] = []
    for part in parts:
        if not part:
            raise KeyError_(f"empty key name in {combo!r}")
        subparts = [part]
        low = part.lower()
        if "-" in part and len(part) > 1 and low not in _NAMED and low not in _PUNCT_NAMES:
            subparts = part.split("-")
            if any(not sp for sp in subparts):
                raise KeyError_(f"empty key name in {combo!r}")
        for sp in subparts:
            for q in _token_to_qcodes(sp, combo):
                if q not in out:
                    out.append(q)
    return out


_JOIN_PLUS = re.compile(r"(?<=\S)\s+\+\s*(?=\S)|(?<=\S)\s*\+\s+(?=\S)")


_REPEAT = re.compile(r"([^*+][^*]*[^*+]|[^*+])\*(\d{1,3})")


def parse_keys(keys: str) -> List[List[str]]:
    """The tool's ``keys`` argument -> list of chords. Whitespace separates *sequential* chords
    (``'esc esc esc'``, ``'down down return'``); ``+`` joins keys pressed together, with or without spaces
    around it (``'ctrl + alt + f2'`` is one chord). A lone ``+`` or ``space`` is that single key."""
    if not isinstance(keys, str) or not keys.strip():
        raise KeyError_("`keys` is empty; pass e.g. 'return', 'escape', 'ctrl+alt+f2', 'f2'")
    stripped = keys.strip()
    if stripped in ("+", "space"):
        return [combo_to_qcodes(stripped)]
    joined = _JOIN_PLUS.sub("+", stripped)
    chords: List[List[str]] = []
    for tok in joined.split():
        m = _REPEAT.fullmatch(tok)     # 'down*3' = down down down
        n = int(m.group(2)) if m else 1
        if n < 1:
            raise KeyError_(f"repeat count must be at least 1 in {tok!r} (to press a key once, just write it)")
        if n > MAX_SEQUENCE:
            raise KeyError_(f"repeat count {n} in {tok!r} is more than {MAX_SEQUENCE} keys in one call; split it")
        chords.extend([combo_to_qcodes(m.group(1) if m else tok)] * n)
    if len(chords) > MAX_SEQUENCE:
        raise KeyError_(f"too many keys in one call ({len(chords)} > {MAX_SEQUENCE}); split the sequence")
    return chords



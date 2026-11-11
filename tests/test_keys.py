import pytest

from barehand import keys


def test_plain_chars_and_shift():
    assert keys.char_to_qcodes("a") == ["a"]
    assert keys.char_to_qcodes("A") == ["shift", "a"]
    assert keys.char_to_qcodes("7") == ["7"]
    assert keys.char_to_qcodes("!") == ["shift", "1"]
    assert keys.char_to_qcodes("_") == ["shift", "minus"]
    assert keys.char_to_qcodes("\n") == ["ret"]
    assert keys.char_to_qcodes(" ") == ["spc"]
    assert keys.char_to_qcodes("|") == ["shift", "backslash"]


def test_every_printable_ascii_maps():
    for code in range(32, 127):
        assert keys.char_to_qcodes(chr(code))


@pytest.mark.parametrize("combo,expected", [
    ("return", ["ret"]), ("Enter", ["ret"]), ("escape", ["esc"]), ("f12", ["f12"]), ("F1", ["f1"]),
    ("delete", ["delete"]), ("del", ["delete"]), ("insert", ["insert"]), ("pageup", ["pgup"]),
    ("pagedown", ["pgdn"]), ("ctrl+alt+delete", ["ctrl", "alt", "delete"]),
    ("ctrl+c", ["ctrl", "c"]), ("shift+tab", ["shift", "tab"]), ("cmd+space", ["meta_l", "spc"]),
    ("option+f2", ["alt", "f2"]), ("ctrl+minus", ["ctrl", "minus"]), ("ctrl++", ["ctrl", "shift", "equal"]),
    ("+", ["shift", "equal"]), ("space", ["spc"]), ("alt+SysRq+b", ["alt", "sysrq", "b"]),
    ("kp_5", ["kp_5"]), ("capslock", ["caps_lock"]),
])
def test_combos(combo, expected):
    assert keys.combo_to_qcodes(combo) == expected


def test_sequences_are_whitespace_separated():
    assert keys.parse_keys("esc esc  esc") == [["esc"]] * 3
    assert keys.parse_keys("down down return") == [["down"], ["down"], ["ret"]]
    assert keys.parse_keys("ctrl+alt+f2") == [["ctrl", "alt", "f2"]]
    assert keys.parse_keys("space") == [["spc"]]


@pytest.mark.parametrize("bad", ["", "   ", "f13", "ctrl+hyper", "banana", "ctrl++x", "a+", "ctrl+", "+x+"])
def test_unknown_keys_raise_helpfully(bad):
    with pytest.raises(keys.KeyError_) as exc:
        keys.parse_keys(bad)
    assert str(exc.value)


def test_unknown_key_message_lists_valid_names():
    with pytest.raises(keys.KeyError_, match="escape"):
        keys.parse_keys("hyperkey")


def test_sequence_length_cap():
    with pytest.raises(keys.KeyError_, match="too many"):
        keys.parse_keys(" ".join(["a"] * (keys.MAX_SEQUENCE + 1)))


def test_text_validates_before_anything_is_sent():
    assert keys.text_to_chords("Hi!\r\n") == [["shift", "h"], ["i"], ["shift", "1"], ["ret"]]
    with pytest.raises(keys.KeyError_, match="U\\+00E9|'é'"):
        keys.text_to_chords("ok é")
    with pytest.raises(keys.KeyError_):
        keys.text_to_chords("tab\x00")


@pytest.mark.parametrize("text,expected", [
    ("ctrl + c", [["ctrl", "c"]]),
    ("ctrl + alt + f2", [["ctrl", "alt", "f2"]]),
    ("ctrl +alt+ f2", [["ctrl", "alt", "f2"]]),
    ("shift + tab", [["shift", "tab"]]),
    ("  ctrl+c  ", [["ctrl", "c"]]),
    ("ctrl-alt-del", [["ctrl", "alt", "delete"]]),
    ("alt-f2", [["alt", "f2"]]),
    ("esc  esc\tesc", [["esc"]] * 3),
    ("ctrl+c ctrl+v", [["ctrl", "c"], ["ctrl", "v"]]),
    ("ctrl + c ctrl + v", [["ctrl", "c"], ["ctrl", "v"]]),
    ("+", [["shift", "equal"]]),
    ("ctrl++", [["ctrl", "shift", "equal"]]),
    ("ctrl + +", [["ctrl", "shift", "equal"]]),
    ("plus", [["shift", "equal"]]),
    ("-", [["minus"]]),
    ("minus", [["minus"]]),
    ("down down return", [["down"], ["down"], ["ret"]]),
])
def test_whitespace_plus_hyphen_and_sequences(text, expected):
    assert keys.parse_keys(text) == expected


def test_qcode_names_are_valid_in_qemu_11():
    from pathlib import Path
    valid = set(Path(__file__).with_name("qkeycodes_qemu11.txt").read_text().split())
    assert len(valid) > 100
    assert keys.ALL_QCODES <= valid, sorted(keys.ALL_QCODES - valid)


def test_repeat_count_must_be_between_one_and_the_maximum():
    import pytest as _pt
    from barehand import keys as k
    assert len(k.parse_keys("down*3 return")) == 4
    with _pt.raises(k.KeyError_, match="at least 1"):
        k.parse_keys("a*0")
    with _pt.raises(k.KeyError_, match=r"999.*64"):
        k.parse_keys("a*999")
    assert len(k.parse_keys("a*64")) == 64
    with _pt.raises(k.KeyError_, match="too many"):
        k.parse_keys("a*40 b*40")
    assert k.parse_keys("shift+8*3") == [["shift", "8"]] * 3

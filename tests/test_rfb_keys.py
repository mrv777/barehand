"""DES vectors, VNC auth, keysym / scancode mapping, address parsing. No sockets."""

import pytest

from barehand import rfb
from barehand import transport_vnc as tv
from barehand.keys import ALL_QCODES, char_to_qcodes, parse_keys, text_to_chords

DES_VECTORS = [   # (key, plaintext, ciphertext): classic worked example and published known-answer tests
    ("133457799BBCDFF1", "0123456789ABCDEF", "85E813540F0AB405"),
    ("0101010101010101", "8000000000000000", "95F8A5E5DD31D900"),
    ("0123456789ABCDEF", "4E6F772069732074", "3FA40E8A984D4815"),     # "Now is t"
    ("0000000000000000", "0000000000000000", "8CA64DE9C1B123A7"),
    ("FFFFFFFFFFFFFFFF", "FFFFFFFFFFFFFFFF", "7359B2163E4EDC58"),
    ("3000000000000000", "1000000000000001", "958E6E627A05557B"),
    ("0113B970FD34F2CE", "059B5E0851CF143A", "86A560F10EC6D85B"),
]


@pytest.mark.parametrize("key,pt,ct", DES_VECTORS)
def test_des_known_answers(key, pt, ct):
    assert rfb.des_encrypt_block(bytes.fromhex(key), bytes.fromhex(pt)).hex().upper() == ct


def test_des_rejects_bad_sizes():
    with pytest.raises(ValueError):
        rfb.des_encrypt_block(b"short", bytes(8))
    with pytest.raises(ValueError):
        rfb.des_encrypt_block(bytes(8), b"x")


def test_vnc_auth_bit_reversal_quirk():
    assert rfb._reverse_bits(0x01) == 0x80 and rfb._reverse_bits(0x61) == 0x86 and rfb._reverse_bits(0xF0) == 0x0F
    ch = bytes(range(16))
    # equals plain DES under the bit-reversed, zero-padded key (the quirk), for each 8-byte half
    key = bytes(rfb._reverse_bits(b) for b in b"secret".ljust(8, b"\0"))
    expect = rfb.des_encrypt_block(key, ch[:8]) + rfb.des_encrypt_block(key, ch[8:])
    assert rfb.vnc_auth_response("secret", ch) == expect
    # without the reversal the answer would differ
    assert rfb.des_encrypt_block(b"secret\0\0", ch[:8]) != expect[:8]


def test_vnc_auth_truncates_to_eight_bytes_and_checks_challenge():
    ch = bytes(16)
    assert rfb.vnc_auth_response("12345678", ch) == rfb.vnc_auth_response("12345678EXTRA", ch)
    assert rfb.vnc_auth_response("", ch) == rfb.vnc_auth_response("\0\0\0", ch)
    with pytest.raises(ValueError):
        rfb.vnc_auth_response("x", bytes(15))


def test_char_keysym_printable_ascii_and_controls():
    for cp in range(0x20, 0x7F):
        assert rfb.char_keysym(chr(cp)) == cp
    assert rfb.char_keysym("\n") == 0xFF0D and rfb.char_keysym("\t") == 0xFF09
    assert rfb.char_keysym("é") == 0xE9 and rfb.char_keysym("€") == 0x010020AC
    with pytest.raises(ValueError):
        rfb.char_keysym("\x01")
    with pytest.raises(ValueError):
        rfb.char_keysym("ab")


def test_named_keysyms():
    assert rfb.KEYSYMS["Return"] == 0xFF0D and rfb.KEYSYMS["Escape"] == 0xFF1B and rfb.KEYSYMS["F1"] == 0xFFBE
    assert rfb.KEYSYMS["F12"] == 0xFFC9 and rfb.KEYSYMS["Shift_L"] == 0xFFE1 and rfb.KEYSYMS["KP_0"] == 0xFFB0


def test_every_plugin_qcode_has_a_mapping():
    have = set(tv.supported_qcodes())
    assert ALL_QCODES <= have
    for q in ALL_QCODES:
        tv.qcode_keysym(q)
        tv.qcode_keysym(q, shifted=True)
    assert [q for q in have if tv.qcode_xt(q) is None] == ["pause"]


def test_scancodes_are_unique_and_sane():
    seen = {}
    for q in tv.supported_qcodes():
        xt = tv.qcode_xt(q)
        if xt is None:
            continue
        assert 0 < xt < 0x100 and xt not in seen, (q, seen.get(xt))
        seen[xt] = q
    assert tv.qcode_xt("a") == 0x1E and tv.qcode_xt("z") == 0x2C and tv.qcode_xt("1") == 0x02
    assert tv.qcode_xt("ret") == 0x1C and tv.qcode_xt("esc") == 0x01 and tv.qcode_xt("spc") == 0x39
    assert tv.qcode_xt("grave_accent") == 0x29 and tv.qcode_xt("backslash") == 0x2B
    assert tv.qcode_xt("up") == 0xC8 and tv.qcode_xt("delete") == 0xD3 and tv.qcode_xt("kp_enter") == 0x9C
    assert tv.qcode_xt("f1") == 0x3B and tv.qcode_xt("f10") == 0x44 and tv.qcode_xt("f11") == 0x57


def test_every_printable_ascii_char_maps_to_its_own_keysym_through_the_chord():
    """chord for the character -> (shift held?, qcode) -> the keysym a real viewer would send is the char itself."""
    for cp in range(0x20, 0x7F):
        ch = chr(cp)
        qs = char_to_qcodes(ch)
        shifted = "shift" in qs
        main = [q for q in qs if q != "shift"][0]
        assert tv.qcode_keysym(main, shifted) == cp, ch
        # the keysym of the unshifted key is what appears without Shift
        if not shifted:
            assert tv.qcode_keysym(main, False) == cp


@pytest.mark.parametrize("text", ["|~_\"", "Hello, World! 0123456789", "!@#$%^&*()_+{}|:\"<>?", "a\tb\nc"])
def test_text_chords_map(text):
    for chord in text_to_chords(text):
        for q in chord:
            tv.qcode_keysym(q, "shift" in chord)


def test_parse_keys_names_all_map():
    for combo in ("ctrl+alt+delete", "ctrl+alt+f2", "esc esc", "alt+tab", "shift+tab", "ctrl+shift+c", "pageup", "kp_enter"):
        for chord in parse_keys(combo):
            for q in chord:
                tv.qcode_keysym(q)


def test_wire_messages():
    assert rfb.RFB.key_msg(0xFF0D, True) == bytes.fromhex("0401 0000 0000ff0d".replace(" ", ""))
    assert rfb.RFB.key_msg(0x41, False) == bytes.fromhex("040000000000" "0041")
    assert rfb.RFB.ext_key_msg(0x61, 0x1E, True) == bytes.fromhex("ff00" "0001" "00000061" "0000001e")
    assert rfb.RFB.pointer_msg(10, 20, 5) == bytes.fromhex("0505000a0014")
    assert rfb.RFB.pointer_msg(-5, 99999, 1) == bytes.fromhex("0501" "0000" "ffff")


@pytest.mark.parametrize("addr,expect", [
    ("127.0.0.1:5900", ("tcp", ("127.0.0.1", 5900))), ("host:1", ("tcp", ("host", 5901))),
    ("host::1", ("tcp", ("host", 1))), ("host", ("tcp", ("host", 5900))), ("[::1]:5901", ("tcp", ("::1", 5901))),
    ("vnc://pikvm.local:5900", ("tcp", ("pikvm.local", 5900))), ("unix:/tmp/v", ("unix", "/tmp/v")),
    (":0", ("tcp", ("127.0.0.1", 5900)))])
def test_parse_address(addr, expect):
    assert rfb.parse_address(addr) == expect


@pytest.mark.parametrize("addr", ["", "a:b", "::::", "h:99999", "http://x/y", "unix:", "fe80::1:5900"])
def test_parse_address_rejects(addr):
    with pytest.raises(ValueError):
        rfb.parse_address(addr)


def test_password_comes_from_a_named_environment_variable():
    assert tv.password_from_env(None) is None and tv.password_from_env("") is None
    assert tv.password_from_env("X_PW", {"X_PW": "pw"}) == "pw"
    with pytest.raises(tv.VNCError, match="X_PW"):
        tv.password_from_env("X_PW", {})


def test_read_secret_uses_hermes_secret_api_when_present_and_environ_otherwise(monkeypatch):
    import sys
    import types
    from barehand import transport_vnc as tv
    monkeypatch.setenv("X_PW", "from-env")
    stub = types.ModuleType("agent.secret_scope")
    seen = []
    stub.get_secret = lambda name, default=None: seen.append(name) or "from-hermes"
    monkeypatch.setitem(sys.modules, "agent.secret_scope", stub)
    assert tv.read_secret("X_PW") == "from-hermes" and seen == ["X_PW"]
    assert tv.read_secret("X_PW", {"X_PW": "explicit"}) == "explicit"      # an explicit mapping bypasses it
    assert tv.password_from_env("X_PW") == "from-hermes"
    stub.get_secret = lambda name, default=None: (_ for _ in ()).throw(RuntimeError("unscoped"))
    assert tv.read_secret("X_PW") == "from-env"                           # an API failure falls back to environ
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)           # import fails: standalone CLI
    assert tv.read_secret("X_PW") == "from-env"
    assert tv.read_secret("NOPE_PW") is None

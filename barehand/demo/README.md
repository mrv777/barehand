# Bundled demo recordings

Both are flight-recorder sessions of `gpt-6.1-terra` driving an aarch64 guest, recorded 2026-11-11.
They need no QEMU, model or network. `hermes barehand demo --list` names them.

- `rescue-grub/` (`hermes barehand demo`): an Alpine VM that stopped at `grub rescue>` (scenario
  `scn-r1-rescue`), repaired in 40 tool calls. An independent judge then booted a copy of the disk with
  no keys sent, and it reached the login prompt (PASS). The last steps in the viewer are the judge's
  captures.
- `debian-install/` (`hermes barehand demo debian`): a Debian 13 install from the network installer on
  a blank machine, 145 tool calls over 29 minutes. The default partitioning failed and the model chose
  another scheme. The first reboot stopped at `grub>`, and the model repaired the boot loader from the
  installer's rescue mode. Afterwards a copy of the disk booted unaided to the login prompt.

Both recordings are unchanged except for three things: the viewer header's description comes from a
`meta` line added to `ledger.js`, the frames were recompressed to 16-colour PNGs, and local paths were
sanitised. Typed text is redacted in the ledger (character counts only), as the recorder does by
default. No host paths remain, and no frame shows a password: the installer and the login prompt mask
those fields. The captures are of a QEMU guest and are MIT licensed, like the rest of the plugin.

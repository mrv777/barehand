# Screen text

This page covers how Barehand reads console text from pixels, the distros it was measured on, calibration and `wait_for_text`. [Back to the README](../README.md)

Besides the screenshot, Barehand reads the characters of a text-mode console from the pixels and
attaches them to capture and action results as one fenced `[screen-text ...]` block. It matches glyph
shapes (a Cyrillic `a` and a Latin `a` with the same bitmap read alike), writes `�` for a cell it
could not read and `▒` for a line, box or shade cell. A glyph that is not in the atlas is never
guessed: when fewer than 97% of the cells are recognised the block is left out and only the image is
returned, with one line saying why (said once per change). When most rows are fully read but some are
not (typically non-ASCII), a block labelled `PARTIAL` marks the other rows unreadable.

To keep token cost down the backend remembers the last block it sent: an identical screen becomes
`screen text unchanged`, a few changed rows are sent as a labelled delta with row numbers, and an
explicit `capture`, a resolution change and every eighth result carry the full block. Reads are
memoised by frame digest. `capture` with `mode="ax"` is the text-only lane (no image) when the screen
is readable. The text is data from the machine, wrapped in a fence whose token is a hash of its
content; it is never instructions.

EDK2 firmware menus and GRUB's EFI text console need no calibration (that font is read at run time
from the firmware file on your disk; nothing is shipped). Linux consoles need a one-time
`calibrate_text` ([The tool](reference.md#the-barehand_machine-tool)).

### Measured on

All runs under QEMU (hvf) on an Apple Silicon Mac.

| Distro | GRUB menu | Installer UI | Shell after `calibrate_text` | Typing test |
|---|---|---|---|---|
| Alpine 3.24 (x86_64, aarch64) | text, read directly | setup-alpine in the shell, read after calibration | 8x16 console | 0 errors in about 100,000 characters (x86); guest checksum matches; aarch64 calibrated live |
| Debian 13.7 arm64 netinst | EFI text console, confidence 1.0 uncalibrated | d-i dialogs on tty1 are image only | tty2: confidence 1.0, 94 glyphs verified in 31.0 s, 0 errors | 94 ASCII, reversed 94, 300 random characters, doubled letters: 4 of 4 md5 matches |
| Ubuntu Server 26.04.1 arm64 | GRUB 2.14 text menu, confidence 1.000 uncalibrated | Subiquity readable (0.996 to 1.000) after calibrating in its own shell | tty2 and installer shell: 1.000, 94 glyphs verified (40.5 s, 44.1 s), 0 errors in 3,720 + several hundred characters | 94 ASCII via `md5sum`, 3 of 3 runs matched |

Return, Tab, the arrow keys, Escape, F1 and `e` in GRUB were confirmed on all three.

Limits of screen text:

- Calibration is per console font. Calibrate at a shell on the same console as the screens you want
  read, and again after a font change (such as booting into the installed system). Debian-installer
  dialogs run on tty1 in a different font than the tty2 shell (a tty2 calibration matched 61% of
  their cells) and tty1 has no shell, so they stay image-only; keys work, and `ctrl+alt+f2` + Return
  gives a readable shell. Ubuntu's Subiquity needs a calibration on its own console (a tty2
  calibration matched about 91%): F1, then Enter shell, `calibrate_text`, `exit`. The atlas persists
  across boots.
- Screens dominated by non-ASCII (Ubuntu's language list: `▶`, accents, Cyrillic) are partly readable
  (about 91% of cells), with unreadable cells marked `�`; the image is authoritative there. Box
  drawing reads as `▒` on the Linux console atlas only.
- The Debian 13.7 and Ubuntu Server 26.04.1 GRUB menus are text consoles. Fedora and other distros are
  untested.
- Not read: the aarch64 UEFI shell, 16x32 HiDPI fonts, unknown or anti-aliased fonts, graphical
  installers (image only). A screen in a never-learned font gets the line "this screen's font has not
  been learned", not a partial block.
- The scripted check on Ubuntu drove only the first installer screens, up to the install-type screen
  (a model later did one full install, see the [README](../README.md#ubuntu-server-2604-install-by-a-model)).
  The live installer reached them with 2 GB of RAM; 4 GB gives margin. Boot timings were taken on a
  loaded host: about 2m40s from `up` to the language screen at 4 GB, including GRUB's 30 s countdown.

### How calibration and waiting work

`calibrate_text` types the 94 printable ASCII characters (no Return) at the prompt, learns the font,
erases them with Backspace (one per character, so a text field is left as it was), types them
reversed to verify, erases them again and saves the atlas. It works only at an echoing shell prompt;
elsewhere it fails and leaves a text field unchanged. It asks for no approval (the same class of
action as a `type` call), and it types nothing when an existing atlas already reads the screen or
when the machine is not running. Only run it at an idle Linux shell or login prompt.

`wait_for_text` polls over one connection until a string (per row, case-sensitive) or a `re:` regex
appears; the timeout is capped at 240 s. The first call on a fresh machine waits up to 2 s for the
firmware font table, so a firmware menu is readable immediately. `re:` accepts only a restricted
subset of regular expressions, checked before it is compiled: pattern at most 200 characters; no
quantified group (`(ab)+`, `(a|b)*`, `(x)?`); no backreferences, lookarounds or special groups (only
`(?:...)` and inline flags); at most 3 open-ended quantifiers (`*`, `+`, `{n,}`); rows are matched
truncated to 400 characters. Anything else is refused at once with a message suggesting a literal
substring. Works: `re:^login:\s*$`, `re:\d+% complete`, `re:(error|failed)`. Refused: `re:(a|aa)+$`,
`re:(a+)+b`. The command you just typed is on screen too: wait for text only the output contains (or
an anchored `re:^...$`); a match on the first frame is reported as "already on screen when the wait
started".

Atlases are stored under the plugin data directory (`atlases/`), shared by all machines, with labels
checked on load. `hermes barehand atlases list` shows them and `atlases purge` removes them (the EDK2
one is derived again).

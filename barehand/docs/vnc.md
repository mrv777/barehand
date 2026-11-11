# Drive any machine over VNC

This page covers the VNC transport: configuration, what works over it and what does not. [Back to the README](../README.md)

VNC is one of Barehand's two transports. Over QMP Barehand also resets into firmware setup, takes
snapshots, ejects media and controls power; VNC covers the screen and input only.

```yaml
plugins:
  entries:
    barehand:
      settings:
        transport: vnc            # or leave empty: a lone vnc_host selects vnc
        vnc_host: 127.0.0.1       # only this host is ever contacted
        vnc_port: 5900
        vnc_password_env: PIKVM_VNC_PW   # NAME of a Hermes secret / env var; never the password. Max 8 chars used (VNC auth)
        # allow_remote: true      # required for a non-loopback host
```

The password is read through Hermes' secret API (`agent.secret_scope.get_secret`) when running inside
Hermes, and from the process environment otherwise (the standalone `hermes barehand` CLI). It is
still configured by variable NAME (`vnc_password_env` / `BAREHAND_VNC_PASSWORD`). Alternatives:
`vnc: "host:5900"` (also `host:N` = display N, `unix:/path`), env `BAREHAND_TRANSPORT`,
`BAREHAND_VNC`. Setting both `qmp` and a VNC address without `transport` is an error.

VNC is not encrypted: the password is weak DES and every keystroke, including typed passwords, is
readable on the wire. Use a trusted network or an SSH tunnel (`ssh -L 5900:127.0.0.1:5900 host`, then
`vnc_host: 127.0.0.1`). Non-loopback hosts are refused unless `allow_remote: true`;
`hermes barehand doctor` warns when it is on.

Over VNC these work: capture, key, type, click, drag, scroll and wait, screen text, effect detection,
the recording ledger, and `barehand_machine` actions `status`, `send_ctrl_alt_del`, `calibrate_text`
and `wait_for_text`. `hermes barehand doctor` reports the transport, password variable name,
reachability (RFB version, security type, desktop name), framebuffer size and whether keys go as raw
scancodes (QEMU) or keysyms (layout must be US). These give a "not available over the VNC transport"
error with no approval prompt: snapshots, reset, power_off, eject_media and
`hermes barehand up/down/eject/reset/snapshot`. VeNCrypt/TLS-only and Tight-only servers, non-US
layouts and pointers without a tablet-class guest device are unsupported.

Unit tests (`tests/test_vnc_wire.py`) drive the backend, tool, CLI and doctor against a fake RFB
server. Live, against QEMU aarch64 (`-vnc 127.0.0.1:90`, Alpine live ISO, HVF), capture returned
1280x800 and typing `root` + Return advanced the login prompt to `Password:`, effect `confirmed`.

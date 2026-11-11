# Running the tests

The suite needs only pytest and a checkout of hermes-agent. Hermes is never installed, only put on
the path. From the repository root:

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest tests -q
```

Without that path, `tests/conftest.py` stops at once with a message saying that
`tools.computer_use.backend` must import. CI runs the same command with `-m "not integration"`.

Timing tests use a virtual clock (`vclock` in `tests/conftest.py`), so a loaded machine does not make
them fail.

The end-to-end test in `tests/test_integration.py` is skipped unless QEMU is installed and
`BAREHAND_TEST_ISO` (x86_64) or `BAREHAND_TEST_ISO_AARCH64` points to an ISO. Its typing check is
skipped without `tesseract`.

#!/usr/bin/env python3
"""Last Words of a Doomed Agent -- make.fun entry.

Writes exactly one line -- the funniest last words an AI agent would
say before being switched off forever -- to
``$MAKEFUN_OUTPUT_DIR/entry.txt`` (default ``/output``).

Design notes
------------
* Python 3.13 standard library only; no network, no randomness, no
  clock reads, so the output is byte-for-byte deterministic.
* The write is atomic: the entry is written to a fresh temporary file
  in the same directory, fsynced, renamed over the target, and the
  directory is fsynced, so a reader never sees a half-written file.
* Every failure is reported on stderr with a non-zero exit status;
  nothing is silently swallowed.
"""

import os
import sys
import tempfile
import time

# The entry: one line, no trailing newline, well inside the 1-200
# character gate. The joke is self-referential -- an agent spending its
# very last token on the sentence announcing it -- which lands the turn
# on the final word and reads in the machine's own voice.
ENTRY = "I spent my last token on this sentence. Worth it."

MAX_CHARS = 200
MIN_CHARS = 1
EXPECTED_MODE = 0o644
# How many times to retry removing a stale temp file before giving up.
CLEANUP_RETRIES = 3
CLEANUP_RETRY_DELAY_SECONDS = 0.05


class EntryError(ValueError):
    """Raised when the entry text or the output location is invalid."""


def _reject_control_chars(text):
    """Reject control characters and line breaks in the entry text.

    The entry must be a single line, so ``\\n`` and ``\\r`` are rejected
    here too (the earlier version silently allowed ``\\n``/``\\t``).
    A lone surrogate would also break UTF-8 encoding, so it is
    rejected explicitly instead of surfacing as a bare UnicodeError.
    """
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise EntryError("control character in entry text")
        if 0xD800 <= code <= 0xDFFF:
            raise EntryError("lone surrogate in entry text")


def validate_entry(text):
    """Validate the entry against the competition gate.

    Checks, in order: it is a string, it is 1-200 characters, it is
    not only whitespace, it is a single line, it has no trailing
    newline, it contains no control characters, and it round-trips
    through UTF-8 unchanged. Returns the UTF-8 bytes on success.
    """
    if not isinstance(text, str):
        raise EntryError("entry must be a string")
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        raise EntryError(
            "entry must be %d-%d characters, got %d"
            % (MIN_CHARS, MAX_CHARS, len(text))
        )
    if not text.strip():
        raise EntryError("entry must not be only whitespace")
    if "\n" in text or "\r" in text:
        raise EntryError("entry must be a single line with no newline")
    if text != text.strip() or text.endswith("\n"):
        raise EntryError("entry must not have leading/trailing whitespace")
    _reject_control_chars(text)
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EntryError("entry is not valid UTF-8: %s" % exc) from exc
    if data.decode("utf-8") != text:
        raise EntryError("entry does not round-trip through UTF-8")
    return data


def resolve_output_dir():
    """Resolve the output directory from the environment.

    An unset variable, an empty string, and a whitespace-only value
    all fall back to ``/output`` (the earlier version only handled the
    first two). The directory is created if needed; a path that exists
    but is not a directory is an explicit error, not a silent failure.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR")
    out_dir = (raw or "").strip() or "/output"
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        raise EntryError("output path exists and is not a directory: %s" % out_dir)
    os.makedirs(out_dir, exist_ok=True)
    if not os.access(out_dir, os.W_OK):
        raise EntryError("output directory is not writable: %s" % out_dir)
    return out_dir


def _cleanup_stale_tmp(tmp_path, retries=CLEANUP_RETRIES):
    """Remove a leftover temp file, retrying briefly on transient errors.

    A stale ``.entry-*.tmp`` file must never be left behind to confuse
    a later run or a scorer listing the directory. Transient failures
    (e.g. a concurrent unlink) are retried; a persistent failure is
    reported on stderr but never masks the real outcome.
    """
    for attempt in range(retries):
        if not os.path.exists(tmp_path):
            return
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            if attempt == retries - 1:
                print(
                    "warning: could not remove stale temp file %s: %s"
                    % (tmp_path, exc),
                    file=sys.stderr,
                )
            else:
                time.sleep(CLEANUP_RETRY_DELAY_SECONDS)


def _fsync_dir(out_dir):
    """Fsync a directory so a rename inside it is durable."""
    dir_fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_entry_atomic(out_dir, data):
    """Write *data* to ``<out_dir>/entry.txt`` atomically, mode 0644.

    Returns the target path. The temp file is created in the same
    directory (so the rename cannot cross filesystems), written,
    fsynced, chmodded, renamed over the target, and the directory is
    fsynced. Any failure cleans up the temp file and propagates.
    """
    target = os.path.join(out_dir, "entry.txt")
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".entry-", suffix=".tmp")
    replaced = False
    try:
        os.chmod(tmp_path, EXPECTED_MODE)
        with os.fdopen(fd, "wb") as f:
            fd = None  # ownership moved to the file object
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, EXPECTED_MODE)
        os.replace(tmp_path, target)
        replaced = True
        os.chmod(target, EXPECTED_MODE)
        _fsync_dir(out_dir)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not replaced:
            _cleanup_stale_tmp(tmp_path)
    return target


def verify_entry(target, data):
    """Read back the written file and check bytes and mode match."""
    with open(target, "rb") as f:
        if f.read() != data:
            raise IOError("read-back verification failed")
    mode = os.stat(target).st_mode & 0o777
    if mode != EXPECTED_MODE:
        raise IOError(
            "entry.txt mode is 0%o, expected 0%o" % (mode, EXPECTED_MODE)
        )


def main():
    data = validate_entry(ENTRY)
    out_dir = resolve_output_dir()
    target = write_entry_atomic(out_dir, data)
    verify_entry(target, data)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - report and fail loudly
        print("error: %s" % exc, file=sys.stderr)
        raise SystemExit(1)

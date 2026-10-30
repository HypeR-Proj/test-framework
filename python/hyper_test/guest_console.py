#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Reassemble guest console bytes without hiding host or guest failures."""

import re
import sys


HOST_LOG = re.compile(rb'<[0-7]>\[[ \t]*[0-9]+\.[0-9]+\] [^\n]*\n')
GUEST_LOG = re.compile(rb'\[[ \t]*[0-9]+\.[0-9]+\] [^\n]*\n')
FAILURE_MARKERS = (
    b'HypeR: fatal', b'kernel panic', b'Kernel panic', b'HypeR KERNEL PANIC',
    b'HypeR crash monitor', b'kernel startup failed', b'watchdog: BUG: soft lockup',
)


def append_console_output(pending, data, *, preserve_cr=False, filter_guest_logs=False):
    """Reassemble console text across complete interleaved host log records.

    The caller archives raw bytes first. Keep unfinished records in pending so
    arbitrary read boundaries do not leak log fragments into prompt matching.
    Check failures before removing records, and again after joining guest text.
    Linux printk filtering is opt-in for tests matching shell responses rather
    than boot diagnostics. Raw guest diagnostics remain in the caller's log.
    """
    pending.extend(data if preserve_cr else data.replace(b'\r', b''))
    if any(marker in pending for marker in FAILURE_MARKERS):
        raise RuntimeError('host or guest kernel failure')
    pending[:] = HOST_LOG.sub(b'', pending)
    # Host records can themselves split a guest panic message. Check the
    # reconstructed guest stream before filtering any guest printk records.
    if any(marker in pending for marker in FAILURE_MARKERS):
        raise RuntimeError('host or guest kernel failure')
    if filter_guest_logs:
        pending[:] = GUEST_LOG.sub(b'', pending)
    if any(marker in pending for marker in FAILURE_MARKERS):
        raise RuntimeError('host or guest kernel failure')


if __name__ == '__main__':
    output = bytearray()
    append_console_output(output, sys.stdin.buffer.read(), preserve_cr=True)
    sys.stdout.buffer.write(output)

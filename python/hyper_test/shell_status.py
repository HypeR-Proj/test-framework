# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Check actual formal-shell statuses rather than incidental error wording."""
import re


def checked(command):
    if isinstance(command, str):
        command = command.encode()
    return command.rstrip(b'\r\n') + b"; printf '\\nHYPER-COMMAND-STATUS=%s\\n' \"$?\"\n"


def status(output):
    found = re.findall(rb'\nHYPER-COMMAND-STATUS=(\d+)\n', output)
    if len(found) != 1:
        raise AssertionError(f'missing or ambiguous shell status: {output!r}')
    return int(found[0])

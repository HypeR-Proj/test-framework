# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Command results, independent of terminal presentation and background logs."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import time
import uuid


class ObservationError(AssertionError):
    """The evidence is incomplete; it cannot establish command success/failure."""


@dataclass(frozen=True)
class Result:
    status: int
    output: bytes

    def require(self, command, *, failed=False):
        if (self.status != 0) != failed:
            raise AssertionError(f'{command}: exit={self.status}, output={self.output!r}')
        return self.output


def decode_report(serial, token):
    """Reject missing/reordered/duplicate chunks and corrupted or truncated bytes."""
    prefix = rb'(?m)^HTR1 ' + token.encode()
    ends = re.findall(prefix + rb' END (-?\d+) (\d+) ([0-9a-f]{16})\n', serial)
    if len(ends) != 1:
        raise ObservationError('missing or ambiguous command report footer')
    status, size, checksum = (int(ends[0][0]), int(ends[0][1]), int(ends[0][2], 16))
    if not -(1 << 31) <= status < (1 << 31) or size > 1024 * 1024:
        raise ObservationError('command report exceeds protocol bounds')
    chunks = re.findall(prefix + rb' DATA (\d+) ([0-9a-f]+)\n', serial)
    data = bytearray()
    for expected, (index, encoded) in enumerate(chunks):
        if int(index) != expected or len(encoded) > 384 or len(encoded) % 2:
            raise ObservationError('missing, duplicate or malformed command chunk')
        data.extend(bytes.fromhex(encoded.decode()))
    digest = 0xcbf29ce484222325
    for byte in status.to_bytes(4, 'little', signed=True) + data:
        digest = ((digest ^ byte) * 0x100000001b3) & ((1 << 64) - 1)
    if len(data) != size or digest != checksum:
        raise ObservationError('command report length/checksum mismatch')
    return Result(status, bytes(data))


class Shell:
    """Execute in the existing formal shell, preserving variables/cwd/capabilities.

    This is for finite, non-interactive commands. Terminal/guest attachment tests
    use Session directly. The group redirects output, not stdin, and runs once.
    The exporter is development-only; neither kernel nor bootstrap has a test ABI.
    """
    def __init__(self, session, *, reporter='/bin/test-report', bracketed_paste=True):
        self.session = session
        self.reporter = shlex.quote(reporter)
        self.bracketed_paste = bracketed_paste
        self.base = '/tmp/htr-' + uuid.uuid4().hex[:8]
        self.function = '__htr_' + uuid.uuid4().hex[:8]
        self.initialized = False
        self.sequence = 0
        self.artifacts = Path(str(session.logfile) + '.commands') / uuid.uuid4().hex
        self.artifacts.mkdir(parents=True)

    def _send(self, source):
        data = source.encode()
        if self.bracketed_paste:
            # Submit one edit, then Enter. Per-character echo can otherwise
            # spend the command deadline behind concurrent kernel diagnostics
            # before the shell has even received the complete command.
            data = b'\x1b[200~' + data.removesuffix(b'\n') + b'\x1b[201~\r'
        self.session.send(data)

    def run(self, command, timeout=60):
        if isinstance(command, bytes):
            command = command.decode()
        self.sequence += 1
        token = uuid.uuid4().hex
        stem = self.artifacts / f'{self.sequence:04}'
        metadata = {'command': command, 'command_token': token, 'status': None}
        stem.with_suffix('.json').write_text(json.dumps(metadata, indent=2) + '\n')
        deadline = time.monotonic() + timeout
        if not self.initialized:
            ready = uuid.uuid4().hex
            # Keep the per-command input short: serial line editing is itself
            # expensive under cross-architecture TCG. No product shell hooks.
            setup = (f'{self.function}() {{ '
                     f'{self.reporter} "$1" {self.base} "$3"; '
                     "printf '\\nHTR1 %s READY %s\\n' \"$2\" \"$?\"; }; "
                     f"printf '\\nHTR1 %s INIT\\n' {ready}\n")
            self._send(setup)
            self.session.await_text(rb'\nHTR1 ' + ready.encode() + rb' INIT\n', timeout=timeout)
            self.initialized = True
        # A newline before } also permits here-documents and trailing comments.
        # Publish a command-bound status only inside the capture group. Failed
        # output redirection or status writes leave no valid result for this
        # command, even when files from the preceding command still exist.
        execute = (f'{{ {{ {command}\n}}; '
                   f'printf "%s %s\\n" {token} "$?" >{self.base}.status; '
                   f'}} >{self.base}.output 2>&1; ')
        last_error = None
        for attempt in range(3):
            if time.monotonic() >= deadline:
                raise TimeoutError(f'command observation deadline expired: {command!r}')
            receipt = uuid.uuid4().hex
            # A delayed frame from an earlier read cannot contaminate a reread.
            report_token = token if attempt == 0 else uuid.uuid4().hex
            report = f'{self.function} {report_token} {receipt} {token}\n'
            # The builtin receipt runs only after the exporter has been joined;
            # input now belongs to the shell, not its exiting child. Splitting
            # the format and token prevents command echo from forging a receipt.
            self._send((execute if attempt == 0 else '') + report)
            try:
                serial = self.session.await_text(
                    rb'\nHTR1 ' + receipt.encode() + rb' READY (\d+)\n',
                    timeout=max(0, deadline - time.monotonic()))
            except TimeoutError as error:
                raise TimeoutError(f'command did not complete: {command!r}: {error}') from error
            if not serial.endswith(b'READY 0\n'):
                raise ObservationError(f'command exporter failed: {serial[-4096:]!r}')
            try:
                result = decode_report(serial, report_token)
                break
            except ObservationError as error:
                last_error = error
                stem.with_suffix(f'.read-{attempt + 1}.serial').write_bytes(serial)
        else:
            raise ObservationError(f'cannot read saved result for {command!r}: {last_error}')
        stem.with_suffix('.out').write_bytes(result.output)
        metadata.update(status=result.status, report_reads=attempt + 1, token=report_token)
        stem.with_suffix('.json').write_text(json.dumps(metadata, indent=2) + '\n')
        return result

    def check(self, command, *, failed=False, timeout=60):
        # A leading boundary preserves line-oriented assertions even when the
        # command's first byte is at offset zero. The payload itself is untouched.
        return b'\n' + self.run(command, timeout).require(command, failed=failed)

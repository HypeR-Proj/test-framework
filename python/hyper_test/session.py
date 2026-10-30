# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Owned serial subprocess transport; guest expectations belong to scenarios."""
import os
import re
import selectors
import subprocess
import time
from pathlib import Path


def native_command(qemu, image, initramfs, *, machine='virt,virtualization=on,gic-version=3'):
    """Serial Native test boot; QEMU_* overrides also select RISC-V platforms.

    Scenarios append device arguments and keep their own expectations/deadlines.
    """
    return [qemu, '-machine', os.environ.get('QEMU_MACHINE', machine),
            '-cpu', os.environ.get('QEMU_CPU', 'max'),
            '-smp', os.environ.get('QEMU_CPUS', '4'),
            '-m', os.environ.get('QEMU_MEMORY', '1G'),
            '-nodefaults', '-display', 'none', '-serial', 'stdio', '-no-reboot',
            '-monitor', 'none', '-kernel', str(image), '-initrd', str(initramfs),
            '-append', os.environ.get('QEMU_BOOTARGS', 'earlycon=pl011,mmio32,0x09000000')]


class Session:
    def __init__(self, command, logfile, *, failures=(b'HypeR: fatal', b'kernel panic',
                 b'Kernel panic', b'HypeR KERNEL PANIC', b'HypeR crash monitor'),
                 cleanup_timeout=3, output_filter=None):
        self.command = command
        self.logfile = logfile
        self.failures = failures
        self.cleanup_timeout = cleanup_timeout
        self.output_filter = output_filter
        self.pending = bytearray()
        self.process = None
        self.selector = None
        self.log = None
        self.eof = False

    def __enter__(self):
        self.log = open(self.logfile, 'wb')
        try:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, kind, error, traceback):
        try:
            if kind is not None:
                # Failure markers can precede their formatted reason on the
                # serial stream. Preserve a bounded raw tail before stopping
                # QEMU, without filtering or replacing the original failure.
                deadline = time.monotonic() + 0.5
                while not self.eof and (remaining := deadline - time.monotonic()) > 0:
                    try:
                        self._read(min(0.05, remaining))
                    except OSError:
                        break
        finally:
            self.close()
        # A successful final assertion can leave a fatal diagnostic unread in
        # the pipe. Cleanup drains it; it must still fail an otherwise good run.
        # Preserve the original exception if the scenario had already failed.
        if kind is None:
            self._failure()

    def close(self):
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=self.cleanup_timeout)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=self.cleanup_timeout)
                # QEMU has no inherited child writers. Drain only currently
                # available bytes so an unexpected inherited pipe cannot hang.
                if self.selector is not None:
                    while self.selector.select(0):
                        if not self._read(0):
                            break
                self.process.stdin.close()
                self.process.stdout.close()
        finally:
            if self.selector is not None:
                self.selector.close()
            if self.log is not None:
                self.log.close()

    def _read(self, timeout):
        progress = False
        for key, _ in self.selector.select(timeout):
            data = os.read(key.fd, 65536)
            if not data:
                self.eof = True
                self.selector.unregister(key.fileobj)
                continue
            progress = True
            self.log.write(data)
            self.log.flush()
            self.pending.extend(data.replace(b'\r', b''))
        return progress

    def _failure(self):
        # Inspect custom failure markers before a scenario filters logs, then
        # check again after reconstructing text split by those records. Keep
        # filtering out of _read so cleanup always drains and closes pipes.
        if any(marker in self.pending for marker in self.failures):
            raise RuntimeError(f'kernel failure: {bytes(self.pending[-4096:])!r}')
        if self.output_filter is not None:
            self.output_filter(self.pending, b'')
        if any(marker in self.pending for marker in self.failures):
            raise RuntimeError(f'kernel failure: {bytes(self.pending[-4096:])!r}')

    def _exited(self):
        code = self.process.poll()
        if code is not None:
            while self._read(0):
                pass
            raise RuntimeError(f'QEMU exited with {code}: {bytes(self.pending[-8192:])!r}')
        if self.eof:
            raise RuntimeError('QEMU closed serial output before exiting')

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            self._read(min(0.02, remaining))
            self._failure()
            self._exited()

    def await_text(self, pattern, timeout=60, *, match_only=False):
        deadline = time.monotonic() + timeout
        while True:
            self._failure()
            match = re.search(pattern, self.pending)
            if match:
                result = match.group(0) if match_only else bytes(self.pending[:match.end()])
                del self.pending[:match.end()]
                return result
            self._exited()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'waiting for {pattern!r}: {bytes(self.pending[-4096:])!r}')
            self._read(min(0.2, remaining))

    def send(self, data):
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def mark(self):
        """Raw-log offset for a fresh asynchronous observation, independent of UI."""
        while self._read(0):
            self._failure()
        return self.log.tell()

    def observe(self, pattern, timeout=60, *, after=0):
        """Wait on archived evidence without consuming terminal expectations.

        Services can finish before or after a prompt/command. Each observer owns
        its cursor; unrelated await_text calls cannot discard its evidence.
        Use mark() before the action when an earlier generation must not match.
        """
        deadline = time.monotonic() + timeout
        pending = bytearray()
        with Path(self.logfile).open('rb') as archive:
            archive.seek(after)
            while True:
                self._failure()
                pending.extend(archive.read().replace(b'\r', b''))
                match = re.search(pattern, pending)
                if match:
                    return match.group(0)
                if len(pending) > 16 * 1024 * 1024:
                    raise RuntimeError('serial observation exceeds 16 MiB; narrow its cursor')
                self._exited()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'log observation {pattern!r} after {after}: '
                                       f'{bytes(pending[-4096:])!r}')
                self._read(min(0.2, remaining))

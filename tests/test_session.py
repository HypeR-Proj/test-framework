#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Exercise serial transport using small controlled child processes, not QEMU."""
from functools import partial
from pathlib import Path
import sys
import tempfile
import time
import unittest

from hyper_test.session import Session
from hyper_test.guest_console import append_console_output


class SessionTests(unittest.TestCase):
    def session(self, script, log, **options):
        return Session([sys.executable, '-u', '-c', script], log, **options)

    def test_raw_log_and_consumed_normalized_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'raw.log'
            with self.session(
                "import sys,time; sys.stdout.buffer.write(b'first\\r\\nsecond\\r\\n'); "
                "sys.stdout.flush(); value=sys.stdin.buffer.readline(); "
                "sys.stdout.buffer.write(value); sys.stdout.flush(); time.sleep(30)", log
            ) as session:
                self.assertEqual(session.await_text(b'first\\n'), b'first\n')
                self.assertEqual(session.await_text(b'second\\n', match_only=True), b'second\n')
                session.send(b'command\n')
                self.assertEqual(session.await_text(b'command\\n'), b'command\n')
            self.assertEqual(log.read_bytes(), b'first\r\nsecond\r\ncommand\n')
            self.assertIsNotNone(session.process.returncode)

    def test_independent_event_observers_survive_prompt_consumption(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.session(
                "import sys,time; print('OLD\\nEVENT\\nhyper-sh$ '); "
                "sys.stdin.readline(); print('NEW\\nEVENT'); time.sleep(30)",
                Path(directory) / 'raw.log'
            ) as session:
                session.await_text(rb'hyper-sh\$ ')
                session.observe(b'EVENT')
                boundary = session.mark()
                session.send(b'next\n')
                session.observe(b'NEW\nEVENT', after=boundary)
                with self.assertRaises(TimeoutError):
                    session.observe(b'OLD', after=boundary, timeout=0.02)
                # Observing did not consume the interactive cursor.
                session.await_text(b'NEW\nEVENT')

    def test_filtered_console_rejoins_ci_wakeup_and_preserves_raw_log(self):
        raw = (b'\nGUEST_WAKE_06<4>[   84.456723] HypeR: '
               b'masked virtual timer PPI without an active vCPU\n\n~ # ')
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'raw.log'
            script = (f'import sys,time; data={raw!r}; '
                      '[ (sys.stdout.buffer.write(bytes([b])), sys.stdout.flush(), '
                      'time.sleep(0.001)) for b in data ]; time.sleep(30)')
            with self.session(script, log, output_filter=append_console_output) as session:
                self.assertEqual(session.await_text(rb'\nGUEST_WAKE_06\n~ # '),
                                 b'\nGUEST_WAKE_06\n~ # ')
            self.assertEqual(log.read_bytes(), raw)

    def test_filter_cannot_hide_custom_failure_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'kernel failure'):
                with self.session(
                    "import time; print('<4>[ 1.0] HypeR: CUSTOM_FAILURE'); time.sleep(30)",
                    Path(directory) / 'log', failures=(b'CUSTOM_FAILURE',),
                    output_filter=append_console_output
                ) as session:
                    session.await_text(b'READY')

    def test_guest_printk_splitting_echo_does_not_block_enter(self):
        raw = (b'\x1b[6necho HYPER_GUEST_CONS'
               b'[    8.346375] hrtimer: interrupt took 2075120 ns\nOLE_RX')
        result = b'\nHYPER_GUEST_CONSOLE_RX\n'
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'raw.log'
            # Await Enter after echo, as BusyBox does. The test must not need
            # a rescue key to advance beyond an interleaved diagnostic.
            script = (f'import sys,time; data={raw!r}; '
                      '[(sys.stdout.buffer.write(bytes([b])), sys.stdout.flush(), '
                      'time.sleep(0.001)) for b in data]; '
                      'assert sys.stdin.buffer.read(1) == b"\\r"; '
                      f'sys.stdout.buffer.write({result!r}); sys.stdout.flush(); time.sleep(30)')
            with self.session(
                script, log,
                output_filter=partial(append_console_output, filter_guest_logs=True)
            ) as session:
                session.await_text(rb'echo HYPER_GUEST_CONSOLE_RX', timeout=5)
                session.send(b'\r')
                session.await_text(rb'\nHYPER_GUEST_CONSOLE_RX\n', timeout=5)
            self.assertEqual(log.read_bytes(), raw + result)

    def test_timeout_reaps_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TimeoutError):
                with self.session('import time; time.sleep(30)', Path(directory) / 'log') as session:
                    session.await_text(b'never', timeout=0.05)
            self.assertIsNotNone(session.process.returncode)
            self.assertTrue(session.process.stdin.closed)
            self.assertTrue(session.process.stdout.closed)

    def test_early_exit_retains_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'log'
            with self.assertRaisesRegex(RuntimeError, 'QEMU exited with 7:.*broken'):
                with self.session("import sys; print('broken'); sys.exit(7)", log) as session:
                    session.process.wait(timeout=3)
                    session.await_text(b'never')
            self.assertEqual(log.read_bytes(), b'broken\n')

    def test_failure_marker_wins_over_success_in_same_read(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'kernel failure'):
                with self.session("import time; print('kernel panic READY'); time.sleep(30)",
                                  Path(directory) / 'log') as session:
                    session.await_text(b'READY')

    def test_failure_cleanup_retains_reason_after_split_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'log'
            with self.assertRaisesRegex(RuntimeError, 'kernel failure'):
                with self.session(
                    "import sys,time; sys.stdout.write('kernel panic '); sys.stdout.flush(); "
                    "time.sleep(0.1); print('full reason'); time.sleep(30)", log
                ) as session:
                    session.await_text(b'READY')
            self.assertIn(b'kernel panic full reason\n', log.read_bytes())
            self.assertIsNotNone(session.process.returncode)
            self.assertTrue(session.log.closed)

    def test_cleanup_checks_fatal_output_after_the_final_assertion(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'log'
            with self.assertRaisesRegex(RuntimeError, 'kernel failure'):
                with self.session("import sys; print('READY'); sys.stdin.readline(); "
                                  "print('kernel panic after READY')", log) as session:
                    session.await_text(b'READY')
                    session.send(b'finish\n')
                    session.process.wait(timeout=3)
                    # Leave the fatal bytes in the pipe for close() to drain.
            self.assertIn(b'kernel panic after READY', log.read_bytes())
            self.assertTrue(session.log.closed)

    def test_exception_cleanup_escalates_to_kill(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'scenario failure'):
                with self.session(
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "print('READY'); time.sleep(30)", Path(directory) / 'log', cleanup_timeout=0.05
                ) as session:
                    session.await_text(b'READY')
                    raise ValueError('scenario failure')
            self.assertLess(session.process.returncode, 0)

    def test_closed_serial_does_not_spin_until_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'closed serial output'):
                with self.session('import os,time; os.close(1); os.close(2); time.sleep(30)',
                                  Path(directory) / 'log') as session:
                    session.await_text(b'never', timeout=10)
            self.assertLess(time.monotonic() - started, 5)


if __name__ == '__main__':
    unittest.main()

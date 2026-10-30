#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Adversarial transport checks and the actual exporter against a host shell."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from hyper_test.command import Shell, ObservationError, decode_report
from hyper_test.journal import Journal
from hyper_test.session import Session


TOKEN = '1' * 32


def frame(data, status=0, token=TOKEN):
    checksum = 0xcbf29ce484222325
    for byte in status.to_bytes(4, 'little', signed=True) + data:
        checksum = ((checksum ^ byte) * 0x100000001b3) & ((1 << 64) - 1)
    chunks = b''.join(f'HTR1 {token} DATA {i} {data[n:n+192].hex()}\n'.encode()
                      for i, n in enumerate(range(0, len(data), 192)))
    return chunks + f'HTR1 {token} END {status} {len(data)} {checksum:016x}\n'.encode()


class ProtocolTests(unittest.TestCase):
    def test_stale_prompts_echo_and_background_logs_are_not_results(self):
        noise = b'hyper-sh$ echo SUCCESS\n<6>[ 1.0] HypeR: background\n'
        encoded = frame(b'hyper-sh$ \x00\x1b[31m\r\n', 7)
        result = decode_report(noise + frame(b'wrong', token='2' * 32)
                               + noise.join(encoded.splitlines(keepends=True)), TOKEN)
        self.assertEqual(result.status, 7)
        self.assertEqual(result.output, b'hyper-sh$ \x00\x1b[31m\r\n')

    def test_incomplete_corrupted_duplicate_and_reordered_evidence_fails(self):
        original = frame(bytes(range(256)) * 2)
        lines = original.splitlines(keepends=True)
        broken = (original[:-1], original.replace(b'000102', b'010102'),
                  original.replace(b'END 0 ', b'END 1 '),
                  b''.join(lines[1:]), b''.join([lines[1], lines[0], *lines[2:]]),
                  lines[0] + original, original + lines[-1])
        for data in broken:
            with self.subTest(data=data), self.assertRaises(ObservationError):
                decode_report(data, TOKEN)

    def test_empty_result_is_not_missing_result(self):
        self.assertEqual(decode_report(frame(b''), TOKEN).output, b'')
        self.assertEqual(decode_report(frame(b'', -17), TOKEN).status, -17)
        with self.assertRaises(ObservationError):
            decode_report(b'hyper-sh$ ', TOKEN)


class HostCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.reporter = str(Path(cls.directory.name) / 'test-report')
        source = Path(__file__).resolve().parents[1] / 'guest/test-report/src/main.rs'
        subprocess.run(['rustc', '--edition=2024', str(source), '-o', cls.reporter], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_persistent_shell_exact_bytes_and_saved_result_reread(self):
        with tempfile.TemporaryDirectory() as directory:
            with Session(['/bin/bash', '--noprofile', '--norc'], Path(directory) / 'serial') as vm:
                shell = Shell(vm, reporter=self.reporter, bracketed_paste=False)
                shell.base = str(Path(directory) / 'saved')
                result = shell.run("value=persistent; printf 'hyper-sh$ \\000\\r\\n'; printf err >&2; false")
                self.assertEqual(result.status, 1)
                self.assertEqual(result.output, b'hyper-sh$ \x00\r\nerr')
                self.assertEqual(shell.run('printf %s "$value"').output, b'persistent')
                # Corrupt only the first transmission. The side effect must not
                # run again when the host re-reads the saved output.
                original = vm.await_text
                count = 0
                delayed = b''
                def corrupt_once(*args, **kwargs):
                    nonlocal count, delayed
                    data = original(*args, **kwargs)
                    count += 1
                    if count == 1:
                        delayed = data
                        return data.replace(b' DATA 0 ', b' DATA 9 ')
                    # Old bytes arriving late must not become duplicate chunks
                    # or a second footer in the next read of the saved result.
                    return delayed + data
                vm.await_text = corrupt_once
                output = shell.run(f'echo once >>{directory}/effects; cat {directory}/effects').output
                self.assertEqual(output, b'once\n')
                self.assertEqual(count, 2)

    def test_capture_failures_cannot_reuse_a_previous_success(self):
        for failure in ('status-write', 'output-open'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                with Session(['/bin/bash', '--noprofile', '--norc'], Path(directory) / 'serial') as vm:
                    shell = Shell(vm, reporter=self.reporter, bracketed_paste=False)
                    shell.base = str(Path(directory) / 'saved')
                    self.assertEqual(shell.run('true').status, 0)
                    # Bash noclobber provides deterministic redirection failure,
                    # including on hosts running the test as root. Existing
                    # files remain readable and contain the preceding result.
                    if failure == 'status-write':
                        command = 'set -C; false'
                    else:
                        vm.send(b'set -C\n')
                        command = f'echo must-not-run >>{directory}/effects'
                    with self.assertRaisesRegex(ObservationError, 'exporter failed'):
                        shell.run(command)
                    self.assertFalse((Path(directory) / 'effects').exists())

    def test_exporter_rejects_incomplete_and_oversized_saved_results(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'saved'
            for status, data in ((None, b'ok'), (TOKEN + ' not-a-status\n', b'ok'),
                                 ('2' * 32 + ' 0\n', b'ok'), (TOKEN + ' 2147483648\n', b'ok'),
                                 (TOKEN + ' 0' + ' ' * 40 + '\n', b'ok'),
                                 (TOKEN + ' 0', b'ok'),
                                 (TOKEN + ' 0\n', b'x' * (1024 * 1024 + 1))):
                base.with_suffix('.output').write_bytes(data)
                if status is not None:
                    base.with_suffix('.status').write_text(status)
                result = subprocess.run([self.reporter, TOKEN, str(base), TOKEN], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b' END ', result.stdout)


class JournalTests(unittest.TestCase):
    def page(self, records, **changes):
        import json
        page = dict(records=records, next=2, newest=2, oldest=1,
                    discarded_records=0, rejected_sources=0, mirror_dropped_bytes=99)
        page.update(changes)
        return json.dumps(page).encode()

    def record(self, sequence, generation=1):
        return dict(sequence=sequence, text='OK', continued=False,
                    source=dict(component='/probe', stream='stdout', generation=generation))

    def test_paginated_snapshot_ignores_mirror_loss_and_pins_upper_cursor(self):
        import json
        first = json.loads(self.page([self.record(1)]))
        first['next'] = 1
        shell = Mock()
        shell.check.side_effect = [json.dumps(first).encode(), self.page([self.record(2)])]
        self.assertEqual(len(Journal(shell).snapshot('/probe')), 2)
        self.assertIn('--through 2', shell.check.call_args.args[0])

    def test_retention_loss_and_wrong_source_fail(self):
        import json
        for key, value in (('discarded_records', 1), ('rejected_sources', 1)):
            page = json.loads(self.page([]))
            page[key] = value
            shell = Mock()
            shell.check.return_value = json.dumps(page).encode()
            with self.assertRaises(ObservationError):
                Journal(shell).snapshot('/probe')
        shell.check.return_value = self.page([self.record(1)])
        with self.assertRaises(ObservationError):
            Journal(shell).snapshot('/other')

    def test_stalled_or_reset_journal_cannot_complete_a_partial_snapshot(self):
        for second in (self.page([], next=1), self.page([], next=1, newest=0, oldest=1)):
            shell = Mock()
            shell.check.side_effect = [self.page([self.record(1)], next=1), second]
            with self.subTest(second=second), self.assertRaises(ObservationError):
                Journal(shell).snapshot('/probe')

    def test_empty_and_filtered_snapshots_reach_the_upper_cursor(self):
        for page in (self.page([], next=0, newest=0), self.page([])):
            shell = Mock()
            shell.check.return_value = page
            self.assertEqual(Journal(shell).snapshot('/probe'), [])

    def test_old_generation_does_not_satisfy_new_instance(self):
        journal = Journal(Mock())
        journal.snapshot = Mock(side_effect=[[self.record(1)], [self.record(1), self.record(2, 2)]])
        self.assertEqual(len(journal.message('/probe', 'OK', generation=2)), 2)
        self.assertEqual(journal.snapshot.call_count, 2)


if __name__ == '__main__':
    unittest.main()

# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import io

from hyper_test.runner import Case, batches, discover, execute, execute_cases, matrix, select, terminate


class RunnerTests(unittest.TestCase):
    def registry(self, root):
        cases = root / 'cases'
        cases.mkdir()
        fixtures = root / 'fixtures.json'
        fixtures.write_text(json.dumps({'version': 1, 'fixtures': {'native': {
            'architectures': ['aarch64', 'riscv64'], 'command': ['make', 'fixture']}}}))
        case = {'version': 1, 'id': 'stress', 'architectures': ['aarch64', 'riscv64'],
                'fixture': 'native', 'command': ['test', '{arch}'], 'variants': [
                    {'id': 'cpu1', 'environment': {'CPUS': '1'}},
                    {'id': 'cpu4', 'environment': {'CPUS': '4'}}]}
        path = cases / 'stress.json'
        path.write_text(json.dumps(case))
        return cases, fixtures, path, case

    def test_architecture_and_cpu_shards_share_only_matching_fixtures(self):
        with tempfile.TemporaryDirectory() as temporary:
            cases, fixtures, _, _ = self.registry(Path(temporary))
            rows, _ = discover(cases, fixtures)
            plan = matrix(rows)
            self.assertEqual(len(plan['cases']['include']), 4)
            self.assertEqual(len(plan['fixtures']['include']), 2)
            self.assertEqual(rows['stress-riscv64-cpu4'].environment, {'CPUS': '4'})

    def test_invalid_case_cannot_silently_reduce_coverage(self):
        for change in ({'fixture': 'missing'}, {'architectures': []},
                       {'architectures': ['aarch64', 'aarch64']}, {'id': '../escape'}, {'variants': []},
                       {'batch': None}, {'batch': '../escape'}, {'batch': {}},
                       {'batch': {'aarch64': 'one'}}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                cases, fixtures, path, data = self.registry(Path(temporary))
                path.write_text(json.dumps({**data, **change}))
                with self.assertRaises(ValueError):
                    discover(cases, fixtures)

    def test_batch_plan_covers_every_variant_once_and_preserves_individual_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            cases, fixtures, path, data = self.registry(Path(temporary))
            path.write_text(json.dumps({**data, 'batch': {'aarch64': 'arm', 'riscv64': 'risc'}}))
            rows, _ = discover(cases, fixtures)
            plan = matrix(rows)['cases']['include']
            self.assertEqual(len(plan), 2)
            self.assertCountEqual([name for batch in plan for name in batch['members']], rows)
            for batch in plan:
                self.assertEqual([case.id for case in select(rows, batch['id'])], batch['members'])
                self.assertEqual(len({case.arch for case in select(rows, batch['id'])}), 1)
                self.assertEqual(batch['fixture'], 'native')
            self.assertEqual(len(select(rows, 'stress', 'aarch64')), 2)
            self.assertEqual(len(select(rows, 'stress-riscv64-cpu4')), 1)
            self.assertFalse(select(rows, 'batch-arm-aarch64', 'riscv64'))

    def test_batch_rejects_mixed_fixture_dependencies_and_ambiguous_ids(self):
        a = Case('a-aarch64', 'a', 'aarch64', '', ['true'], {}, 10, 'shared')
        b = Case('b-aarch64', 'b', 'aarch64', 'native', ['true'], {}, 10, 'shared')
        with self.assertRaises(ValueError):
            batches({a.id: a, b.id: b})
        collision = Case('batch-shared-aarch64', 'batch-shared', 'aarch64', '', ['true'], {}, 10)
        with self.assertRaises(ValueError):
            batches({a.id: a, collision.id: collision})

    def test_batch_continues_after_failure_without_leaking_environment_or_overwriting_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = ('import os; from pathlib import Path; '
                      'Path("serial.log").write_text(os.environ.get("TOPOLOGY", "default")); '
                      'raise SystemExit(int(os.environ.get("FAIL", "0")))')
            cases = [Case('first', 'first', 'aarch64', '', [sys.executable, '-c', script],
                          {'TOPOLOGY': 'gic2', 'FAIL': '7'}, 10, 'shared'),
                     Case('second', 'second', 'aarch64', '', [sys.executable, '-c', script], {}, 10, 'shared')]
            records = execute_cases(cases, {}, root, root, dict(os.environ), ['{out}/serial.log'])
            self.assertEqual([r['status'] for r in records], ['failed', 'passed'])
            self.assertEqual([r['returncode'] for r in records], [7, 0])
            self.assertEqual([(root / r['diagnostics'][0]).read_text() for r in records], ['gic2', 'default'])
            for record in records:
                self.assertEqual(json.loads((root / 'results' / (record['id'] + '.json')).read_text()), record)

    def test_diagnostics_cannot_follow_links_outside_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'out'
            output.mkdir()
            (root / 'outside.log').write_text('private')
            (output / 'serial.log').symlink_to(root / 'outside.log')
            record = execute('escape', ['true'], root, output / 'results', dict(os.environ), 10,
                             [str(output / '*.log')])
            self.assertEqual(record['status'], 'error')
            self.assertEqual(record['failure_stage'], 'prepare')
            self.assertIn('escapes', record['failure_reason'])
            self.assertNotIn('diagnostics', record)
            self.assertEqual((output / record['stdout']).read_bytes(), b'')

    def test_required_fixture_is_checked_for_each_batch_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = Case('fixture-aarch64', 'fixture', 'aarch64', 'native', ['true'], {}, 10)
            with self.assertRaisesRegex(ValueError, 'required fixture'):
                execute_cases([case], {'native': {}}, root, root,
                              {**os.environ, 'CI_REQUIRE_FIXTURES': '1'})
            record = json.loads((root / 'results/fixture-aarch64.json').read_text())
            self.assertEqual(record['failure_stage'], 'fixture')
            self.assertEqual(record['status'], 'error')

    def test_missing_executable_does_not_skip_the_next_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = [Case('missing', 'missing', 'aarch64', '', [str(root / 'absent')], {}, 10),
                     Case('valid', 'valid', 'aarch64', '',
                          [sys.executable, '-c', 'print("still running")'], {}, 10)]
            records = execute_cases(cases, {}, root, root, dict(os.environ))
            self.assertEqual([r['status'] for r in records], ['error', 'passed'])
            self.assertEqual(records[0]['failure_stage'], 'launch')
            self.assertIn('absent', records[0]['failure_reason'])
            for record in records:
                saved = json.loads((root / 'results' / (record['id'] + '.json')).read_text())
                self.assertEqual(saved, record)
                self.assertTrue((root / record['stdout']).is_file())
                self.assertTrue((root / record['stderr']).is_file())
            self.assertEqual((root / records[1]['stdout']).read_bytes(), b'still running\n')

    def test_timeout_keeps_binary_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = 'import os,time; os.write(1, b"out\\xff"); os.write(2, b"err\\xfe"); time.sleep(60)'
            record = execute('partial-output', [sys.executable, '-c', script],
                             root, root / 'results', dict(os.environ), 0.3)
            self.assertEqual(record['status'], 'timeout')
            self.assertEqual(record['failure_stage'], 'run')
            self.assertEqual((root / record['stdout']).read_bytes(), b'out\xff')
            self.assertEqual((root / record['stderr']).read_bytes(), b'err\xfe')

    def test_bad_template_is_a_local_preparation_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = [Case('bad', 'bad', 'aarch64', '', ['{unknown}'], {}, 10),
                     Case('valid', 'valid', 'aarch64', '', [sys.executable, '-c', 'pass'], {}, 10)]
            records = execute_cases(cases, {}, root, root, dict(os.environ))
            self.assertEqual([r['status'] for r in records], ['error', 'passed'])
            self.assertEqual(records[0]['failure_stage'], 'prepare')

    def test_repeated_case_keeps_distinct_transcripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = [execute('repeat', [sys.executable, '-c', f'print({number})'], root,
                               root / 'results', dict(os.environ), 10) for number in (1, 2)]
            self.assertNotEqual(records[0]['stdout'], records[1]['stdout'])
            self.assertEqual([(root / r['stdout']).read_text() for r in records], ['1\n', '2\n'])

    def test_large_output_is_saved_and_relayed_without_pipe_deadlock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout, stderr = io.StringIO(), io.StringIO()
            script = 'import os; os.write(1, b"a" * 2000000); os.write(2, b"b" * 2000000)'
            with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
                record = execute('large', [sys.executable, '-c', script], root,
                                 root / 'results', dict(os.environ), 10)
            self.assertEqual(record['status'], 'passed')
            self.assertEqual((root / record['stdout']).read_bytes(), b'a' * 2000000)
            self.assertEqual((root / record['stderr']).read_bytes(), b'b' * 2000000)
            self.assertIn('a' * 2000000, stdout.getvalue())
            self.assertEqual(stderr.getvalue(), 'b' * 2000000)

    def test_new_escaping_diagnostic_fails_case_but_continues_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'out'
            output.mkdir()
            (root / 'private').write_text('private')
            script = 'from pathlib import Path; Path("out/serial.log").symlink_to("../private")'
            cases = [Case('bad-log', 'bad-log', 'aarch64', '', [sys.executable, '-c', script], {}, 10),
                     Case('later', 'later', 'aarch64', '', [sys.executable, '-c', 'pass'], {}, 10)]
            # The first member creates an escaping path only after preparation.
            records = execute_cases(cases, {}, root, output, dict(os.environ), ['{out}/serial.log'])
            self.assertEqual([r['status'] for r in records], ['error', 'error'])
            self.assertEqual(records[0]['failure_stage'], 'evidence')
            self.assertEqual(records[1]['failure_stage'], 'prepare')
            self.assertNotIn('diagnostics', records[0])

    def test_timeout_kills_descendants_before_the_next_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # The parent kills and reaps its TERM-resistant child on shutdown;
            # the runner must allow that cleanup before returning to the batch.
            script = '''
import os, signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c',
    'import os,signal,time; from pathlib import Path; '
    'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'Path("child-pid").write_text(str(os.getpid())); time.sleep(60)'])
def finish(*args):
    child.kill()
    child.wait()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, finish)
while not Path('child-pid').exists():
    time.sleep(0.01)
Path('parent-pid').write_text(str(os.getpid()))
time.sleep(60)
'''
            record = execute('tree', [sys.executable, '-c', script], root,
                             root / 'results', dict(os.environ), 0.5)
            self.assertEqual(record['status'], 'timeout')
            for file in ('parent-pid', 'child-pid'):
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((root / file).read_text()), 0)

    def test_cleanup_failure_records_error_and_aborts_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = [Case(name, name, 'aarch64', '', [sys.executable, '-c', 'pass'], {}, 10)
                     for name in ('first', 'later')]

            def failed_cleanup(process):
                terminate(process)
                raise PermissionError('cleanup failed')

            with patch('hyper_test.runner.terminate', side_effect=failed_cleanup):
                with self.assertRaisesRegex(PermissionError, 'cleanup failed'):
                    execute_cases(cases, {}, root, root, dict(os.environ))
            record = json.loads((root / 'results/first.json').read_text())
            self.assertEqual(record['status'], 'error')
            self.assertEqual(record['failure_stage'], 'cleanup')
            self.assertFalse((root / 'results/later.json').exists())

    def test_interruption_saves_evidence_and_stops_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = ('import os,signal,time; from pathlib import Path; '
                     'Path("pid").write_text(str(os.getpid())); '
                     'os.write(1, b"before interrupt\\n"); '
                     'os.kill(os.getppid(), signal.SIGINT); time.sleep(60)')
            wrapper = '''
import os, sys
from pathlib import Path
from hyper_test.runner import Case, execute_cases
root = Path(sys.argv[1])
cases = [Case('interrupt', 'interrupt', 'aarch64', '', [sys.executable, '-c', sys.argv[2]], {}, 10),
         Case('later', 'later', 'aarch64', '', [sys.executable, '-c', 'pass'], {}, 10)]
try:
    execute_cases(cases, {}, root, root, dict(os.environ))
except KeyboardInterrupt:
    raise SystemExit(130)
'''
            environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'python')}
            result = subprocess.run([sys.executable, '-c', wrapper, str(root), child],
                                    env=environment, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 130, result.stderr)
            record = json.loads((root / 'results/interrupt.json').read_text())
            self.assertEqual(record['status'], 'interrupted')
            self.assertEqual(record['failure_stage'], 'run')
            self.assertEqual((root / record['stdout']).read_bytes(), b'before interrupt\n')
            self.assertFalse((root / 'results/later.json').exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(int((root / 'pid').read_text()), 0)

    def test_exit_status_and_timing_are_recorded_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = execute('failure', [sys.executable, '-c', 'raise SystemExit(7)'],
                             root, root / 'results', dict(os.environ), 10)
            self.assertEqual(record['status'], 'failed')
            self.assertEqual(record['returncode'], 7)
            saved = json.loads((root / 'results/failure.json').read_text())
            self.assertEqual(record, saved)
            self.assertGreater(saved['duration_seconds'], 0)

    def test_timeout_reaps_the_owned_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = 'import os,time; open("pid", "w").write(str(os.getpid())); time.sleep(60)'
            record = execute('timeout', [sys.executable, '-c', script], root,
                             root / 'results', dict(os.environ), 0.2)
            self.assertEqual(record['status'], 'timeout')
            with self.assertRaises(ProcessLookupError):
                os.kill(int((root / 'pid').read_text()), 0)


if __name__ == '__main__':
    unittest.main()

# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from hyper_test.runner import discover, execute, matrix


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
                       {'architectures': ['aarch64', 'aarch64']}, {'id': '../escape'}, {'variants': []}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                cases, fixtures, path, data = self.registry(Path(temporary))
                path.write_text(json.dumps({**data, **change}))
                with self.assertRaises(ValueError):
                    discover(cases, fixtures)

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

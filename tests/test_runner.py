# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from hyper_test.runner import Case, batches, discover, execute, execute_cases, matrix, select


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
            with self.assertRaises(ValueError):
                execute('escape', ['true'], root, output / 'results', dict(os.environ), 10,
                        [str(output / '*.log')])

    def test_required_fixture_is_checked_for_each_batch_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = Case('fixture-aarch64', 'fixture', 'aarch64', 'native', ['true'], {}, 10)
            with self.assertRaisesRegex(ValueError, 'required fixture'):
                execute_cases([case], {'native': {}}, root, root,
                              {**os.environ, 'CI_REQUIRE_FIXTURES': '1'})

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

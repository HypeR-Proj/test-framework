#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""List or execute system cases locally and in GitHub Actions."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'python'))
sys.path.insert(0, str(HERE.parent / 'scripts'))

from hyper_test.runner import discover, execute, matrix
from buildsystem.model import discover as modules, relative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=HERE.parent)
    parser.add_argument('--out', type=Path)
    parser.add_argument('action', choices=('list', 'run', 'fixture'))
    parser.add_argument('name', nargs='?')
    parser.add_argument('--arch')
    args = parser.parse_args()
    root = args.workspace.resolve()
    owners = [m for m in modules(root).values() if 'system_tests' in m.metadata.get('workspace', {})]
    if len(owners) != 1:
        raise ValueError('the manifest must declare one system-test registry owner')
    owner = owners[0]
    registry = owner.metadata['workspace']['system_tests']
    cases, fixtures = discover(owner.source / relative(registry['cases']),
                               owner.source / relative(registry['fixtures']))
    if args.action == 'list':
        print(json.dumps(matrix(cases), separators=(',', ':')))
        return
    if args.action == 'run' and args.arch is None:
        args.arch = cases[args.name].arch if args.name in cases else os.environ.get('ARCH', 'aarch64')
    if not os.environ.get('HYPER_BUILD_VIEW'):
        command = [sys.executable, str(root / 'project.py'), '--workspace', str(root)]
        if args.out:
            command.extend(('--out', str(args.out.resolve())))
        command.extend(('exec', '--', sys.executable, str(HERE.relative_to(root) / 'runner.py'), args.action))
        if args.name:
            command.append(args.name)
        if args.arch:
            command.extend(('--arch', args.arch))
        environment = {**os.environ, **({'ARCH': args.arch} if args.arch else {})}
        subprocess.run(command, cwd=root, env=environment, check=True)
        return
    output = Path(os.environ['HYPER_BUILD_OUTPUT'])
    if args.action == 'fixture':
        if args.name not in fixtures or args.arch not in fixtures[args.name]['architectures']:
            raise ValueError('select an available fixture and architecture')
        fixture = output / 'fixtures' / f'{args.name}-{args.arch}'
        values = {'workspace': str(root), 'out': str(output), 'arch': args.arch, 'fixture': str(fixture)}
        command = [value.format_map(values) for value in fixtures[args.name]['command']]
        environment = {**os.environ, **fixtures[args.name].get('environment', {}),
                       'CI_ARCH': args.arch, 'CI_NATIVE_FIXTURE_OUTPUT': str(fixture)}
        record = execute(f'fixture-{args.name}-{args.arch}', command, root, output / 'results', environment, 3600)
        if record['status'] != 'passed':
            raise ValueError('fixture construction failed')
        return
    selected = [case for case in cases.values() if (args.name in (case.id, case.name))
                and (args.arch is None or args.arch == case.arch)]
    if not selected:
        raise ValueError('no matching system case; use list to inspect available names')
    for case in selected:
        fixture = output / 'fixtures' / f'{case.fixture}-{case.arch}'
        values = {'workspace': str(root), 'out': str(output), 'arch': case.arch, 'fixture': str(fixture)}
        environment = {**os.environ, **case.environment, 'CI_ARCH': case.arch}
        if case.fixture and os.environ.get('CI_REQUIRE_FIXTURES') == '1' and not fixture.is_dir():
            raise ValueError(f'{case.id}: required fixture was not downloaded: {fixture}')
        if case.fixture and fixture.is_dir():
            environment.update(fixtures[case.fixture].get('environment', {}))
            environment['CI_NATIVE_FIXTURE'] = str(fixture)
        command = [value.format_map(values) for value in case.command]
        record = execute(case.id, command, root, output / 'results', environment, case.timeout)
        if record['status'] != 'passed':
            raise ValueError(f'{case.id}: {record["status"]}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f'system tests: {error}')

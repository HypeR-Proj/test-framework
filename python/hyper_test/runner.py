# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Discover case-owned commands and record bounded, isolated executions."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

NAME = re.compile(r'[a-z0-9][a-z0-9-]*\Z')


def identifier(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(f'invalid case identifier: {value!r}')
    return value


def command(value):
    if not isinstance(value, list) or not value or any(not isinstance(arg, str) or '\0' in arg for arg in value):
        raise ValueError('case commands must be nonempty argument arrays')
    return value


@dataclass(frozen=True)
class Case:
    id: str
    name: str
    arch: str
    fixture: str
    command: list
    environment: dict
    timeout: int

    def matrix(self):
        return {'id': self.id, 'arch': self.arch, 'fixture': self.fixture}


def discover(directory, fixtures):
    definitions = json.loads(Path(fixtures).read_bytes())
    if definitions.get('version') != 1:
        raise ValueError('unsupported fixture registry version')
    available = definitions['fixtures']
    cases = {}
    for path in sorted(Path(directory).glob('*.json')):
        data = json.loads(path.read_bytes())
        if data.get('version') != 1:
            raise ValueError(f'{path}: unsupported case version')
        name = identifier(data['id'])
        args = command(data['command'])
        architectures = data['architectures']
        if not architectures or len(architectures) != len(set(architectures)):
            raise ValueError(f'{name}: architectures must be unique and nonempty')
        fixture = data.get('fixture', '')
        if fixture and fixture not in available:
            raise ValueError(f'{name}: unknown fixture {fixture}')
        timeout = data.get('timeout_seconds', 3600)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError(f'{name}: invalid timeout')
        variants = data.get('variants', [{}])
        if not isinstance(variants, list) or not variants:
            raise ValueError(f'{name}: variants must be nonempty')
        for arch in architectures:
            identifier(arch)
            if fixture and arch not in available[fixture]['architectures']:
                raise ValueError(f'{name}: fixture does not support {arch}')
            for variant in variants:
                suffix = '-' + identifier(variant['id']) if variant.get('id') else ''
                key = f'{name}-{arch}{suffix}'
                environment = {**data.get('environment', {}), **variant.get('environment', {})}
                if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) or not isinstance(v, str)
                       for k, v in environment.items()):
                    raise ValueError(f'{name}: invalid environment')
                if key in cases:
                    raise ValueError(f'duplicate case: {key}')
                cases[key] = Case(key, name, arch, fixture, args, environment, timeout)
    if not cases:
        raise ValueError('system acceptance has no cases')
    for name, fixture in available.items():
        identifier(name)
        command(fixture['command'])
        architectures = fixture['architectures']
        if not architectures or len(architectures) != len(set(architectures)):
            raise ValueError(f'{name}: fixture architectures must be unique and nonempty')
        for architecture in architectures:
            identifier(architecture)
        for key, value in fixture.get('environment', {}).items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or not isinstance(value, str):
                raise ValueError(f'{name}: invalid fixture environment')
    return cases, available


def matrix(cases):
    fixtures = sorted({(case.fixture, case.arch) for case in cases.values() if case.fixture})
    return {'cases': {'include': [case.matrix() for case in cases.values()]},
            'fixtures': {'include': [{'id': name + '-' + arch, 'name': name, 'arch': arch}
                                     for name, arch in fixtures]}}


def terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def execute(name, arguments, workspace, output, environment, timeout):
    started = time.monotonic()
    record = {'id': name, 'started': datetime.now(timezone.utc).isoformat(), 'command': arguments,
              'status': 'error', 'returncode': None}
    process = None
    try:
        print(f'Running {name}', flush=True)
        process = subprocess.Popen(arguments, cwd=workspace, env=environment, start_new_session=True)
        try:
            record['returncode'] = process.wait(timeout=timeout)
            record['status'] = 'passed' if record['returncode'] == 0 else 'failed'
        except subprocess.TimeoutExpired:
            record['status'] = 'timeout'
        return record
    finally:
        if process is not None:
            terminate(process)
        record['duration_seconds'] = round(time.monotonic() - started, 3)
        output.mkdir(parents=True, exist_ok=True)
        path = output / (identifier(name) + '.json')
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(record, indent=2) + '\n')
        temporary.replace(path)
        print(f'{name}: {record["status"]}, {record["duration_seconds"]:.3f}s', flush=True)

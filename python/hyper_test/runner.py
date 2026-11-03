# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Discover case-owned commands and record bounded, isolated executions."""

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import ExitStack
import json
import glob
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
import tempfile

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
    batch: str = ''

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
                batch = variant.get('batch', data.get('batch', ''))
                if isinstance(batch, dict):
                    if set(batch) != set(architectures):
                        raise ValueError(f'{name}: batch map must cover exactly the case architectures')
                    batch = batch[arch]
                if batch != '':
                    identifier(batch)
                cases[key] = Case(key, name, arch, fixture, args, environment, timeout, batch)
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
    batches(cases)  # Reject ambiguous or incompatible execution groups early.
    return cases, available


def batches(cases):
    groups = {}
    for case in cases.values():
        key = f'batch-{case.batch}-{case.arch}' if case.batch else case.id
        if case.batch and key in cases:
            raise ValueError(f'batch identifier collides with a case: {key}')
        group = groups.setdefault(key, [])
        if group and (group[0].arch, group[0].fixture) != (case.arch, case.fixture):
            raise ValueError(f'{key}: batch cases must share architecture and fixture')
        group.append(case)
    return groups


def select(cases, name, arch=None):
    groups = batches(cases)
    selected = ([cases[name]] if name in cases else
                groups.get(name, [case for case in cases.values() if case.name == name]))
    return [case for case in selected if arch is None or case.arch == arch]


def matrix(cases):
    fixtures = sorted({(case.fixture, case.arch) for case in cases.values() if case.fixture})
    groups = batches(cases)
    return {'cases': {'include': [dict(group[0].matrix(), id=name,
                                     members=[case.id for case in group])
                                  for name, group in groups.items()]},
            'fixtures': {'include': [{'id': name + '-' + arch, 'name': name, 'arch': arch}
                                     for name, arch in fixtures]}}


def terminate(process):
    interrupted = False
    while True:
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL if interrupted else signal.SIGTERM)
            except ProcessLookupError:
                pass
            if not interrupted:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return interrupted
        except KeyboardInterrupt:
            # Preserve the interruption, but finish killing and reaping owned
            # processes before allowing it to abort the batch.
            interrupted = True


def diagnostic_snapshot(patterns, root):
    root = root.resolve()
    found = {}
    for pattern in patterns:
        for filename in glob.iglob(pattern, recursive=True):
            path = Path(filename)
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise ValueError(f'diagnostic escapes the output directory: {path}')
            info = resolved.stat()
            found[resolved] = (info.st_dev, info.st_ino, info.st_size,
                               info.st_mtime_ns, info.st_ctime_ns)
    return found


def preserve_diagnostics(name, patterns, before, output):
    changed = [path for path, stamp in diagnostic_snapshot(patterns, output.parent).items()
               if before.get(path) != stamp]
    if not changed:
        return []
    directory = output / 'diagnostics'
    directory.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix=name + '-', dir=directory))
    saved = []
    for path in sorted(changed):
        copy = destination / path.relative_to(output.parent.resolve())
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copy)
        saved.append(str(copy.relative_to(output.parent)))
    return saved


def execute_cases(selected, fixtures, workspace, output, environment, diagnostics=()):
    records = []
    for case in selected:
        fixture = output / 'fixtures' / f'{case.fixture}-{case.arch}'
        values = {'workspace': str(workspace), 'out': str(output), 'arch': case.arch,
                  'fixture': str(fixture)}
        # Each case gets a fresh environment; QEMU topology and fault controls
        # must not leak from an earlier member sharing this build directory.
        env = {**environment, **case.environment, 'CI_ARCH': case.arch}
        if case.fixture and env.get('CI_REQUIRE_FIXTURES') == '1' and not fixture.is_dir():
            reason = f'{case.id}: required fixture was not downloaded: {fixture}'
            execute(case.id, case.command, workspace, output / 'results', env,
                    case.timeout, preparation_failure=('fixture', reason))
            raise ValueError(reason)
        if case.fixture and fixture.is_dir():
            env.update(fixtures[case.fixture].get('environment', {}))
            env['CI_NATIVE_FIXTURE'] = str(fixture)
        try:
            command = [value.format_map(values) for value in case.command]
            patterns = [p.format_map(values) for p in diagnostics]
        except (KeyError, ValueError) as error:
            records.append(execute(case.id, case.command, workspace, output / 'results', env,
                                   case.timeout, preparation_failure=('prepare', str(error))))
            continue
        records.append(execute(case.id, command, workspace, output / 'results', env,
                               case.timeout, patterns))
    return records


def relay_output(streams):
    # Children write directly to regular files: no pipe capacity or reader thread
    # can hold process cleanup hostage. Bound each live-console pass for fairness.
    copied = False
    for source, console in streams:
        data = source.read(65536)
        if data:
            copied = True
            binary = getattr(console, 'buffer', None)
            if binary is not None:
                binary.write(data)
                binary.flush()
            else:
                console.write(data.decode('utf-8', errors='replace'))
                console.flush()
    return copied


def execute(name, arguments, workspace, output, environment, timeout, diagnostics=(),
            preparation_failure=None):
    identifier(name)
    output = Path(output).resolve()
    started = time.monotonic()
    record = {'id': name, 'started': datetime.now(timezone.utc).isoformat(), 'command': arguments,
              'status': 'error', 'returncode': None}
    process = None
    before = None
    # An unusable results directory is a shared infrastructure failure. Do not
    # continue a batch that cannot durably record which cases were attempted.
    output.mkdir(parents=True, exist_ok=True)
    logs = output / 'logs'
    logs.mkdir(exist_ok=True)
    transcript = Path(tempfile.mkdtemp(prefix=name + '-', dir=logs))
    with ExitStack() as files:
        writers = []
        streams = []
        for channel, console in (('stdout', sys.stdout), ('stderr', sys.stderr)):
            path = transcript / (channel + '.log')
            writers.append(files.enter_context(path.open('wb', buffering=0)))
            streams.append((files.enter_context(path.open('rb', buffering=0)), console))
            record[channel] = str(path.relative_to(output.parent))
        stage = 'prepare'
        try:
            if preparation_failure is not None:
                stage, reason = preparation_failure
                raise ValueError(reason)
            before = diagnostic_snapshot(diagnostics, output.parent)
            print(f'Running {name}', flush=True)
            stage = 'launch'
            process = subprocess.Popen(arguments, cwd=workspace, env=environment,
                                       start_new_session=True, stdout=writers[0], stderr=writers[1])
            stage = 'run'
            deadline = time.monotonic() + timeout
            while True:
                relay_output(streams)
                remaining = deadline - time.monotonic()
                if process.poll() is not None:
                    record['returncode'] = process.returncode
                    record['status'] = 'passed' if process.returncode == 0 else 'failed'
                    if process.returncode != 0:
                        record.update(failure_stage=stage, failure_reason=f'exit status {process.returncode}')
                    break
                if remaining <= 0:
                    record.update(status='timeout', failure_stage=stage,
                                  failure_reason=f'exceeded {timeout} seconds')
                    break
                try:
                    process.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
        except KeyboardInterrupt:
            record.update(status='interrupted', failure_stage=stage, failure_reason='user interruption')
            raise
        except (OSError, ValueError) as error:
            record.update(status='error', failure_stage=stage, failure_reason=str(error))
        finally:
            cleaned = False
            cleanup_interrupted = False
            try:
                if process is not None:
                    cleanup_interrupted = terminate(process)
                    if cleanup_interrupted:
                        record.update(status='interrupted', failure_stage='cleanup',
                                      failure_reason='user interruption')
                cleaned = True
            except OSError as error:
                record['cleanup_error'] = str(error)
                if record['status'] != 'interrupted':
                    record.update(status='error', failure_stage='cleanup', failure_reason=str(error))
                # A failed cleanup can leave an owned process behind. Abort the
                # batch after recording evidence rather than run another case.
                raise
            finally:
                try:
                    while cleaned and relay_output(streams):
                        pass
                    if diagnostics and before is not None:
                        record['diagnostics'] = preserve_diagnostics(name, diagnostics, before, output)
                except (OSError, ValueError) as error:
                    record['evidence_error'] = str(error)
                    if record['status'] not in ('error', 'interrupted'):
                        record.update(status='error', failure_stage='evidence', failure_reason=str(error))
                except KeyboardInterrupt:
                    cleanup_interrupted = True
                    record.update(status='interrupted', failure_stage='evidence',
                                  failure_reason='user interruption')
                while True:
                    try:
                        record['duration_seconds'] = round(time.monotonic() - started, 3)
                        path = output / (name + '.json')
                        temporary = path.with_suffix('.json.tmp')
                        temporary.write_text(json.dumps(record, indent=2) + '\n')
                        temporary.replace(path)
                        break
                    except KeyboardInterrupt:
                        cleanup_interrupted = True
                        record.update(status='interrupted', failure_stage='evidence',
                                      failure_reason='user interruption')
                print(f'{name}: {record["status"]}, {record["duration_seconds"]:.3f}s', flush=True)
            if cleanup_interrupted:
                raise KeyboardInterrupt
    return record

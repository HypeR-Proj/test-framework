# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Bounded service-log snapshots with explicit attribution and evidence loss."""
import json
import shlex
import time

from hyper_test.command import ObservationError


class Journal:
    def __init__(self, shell):
        self.shell = shell

    def snapshot(self, component, *, timeout=30):
        deadline = time.monotonic() + timeout
        after, through = 0, None
        records = []
        while True:
            command = f'journalctl --json -c {shlex.quote(component)} --after {after}'
            if through is not None:
                command += f' --through {through}'
            page = json.loads(self.shell.check(command, timeout=max(0, deadline - time.monotonic())))
            # Retention is global. Conservatively reject loss even if it might
            # concern another component; a bounded journal is not a full archive.
            if page['discarded_records'] or page['rejected_sources']:
                raise ObservationError(f'journal evidence lost: {page}')
            # Mirror drops do not mean journal records were lost.
            if through is None:
                through = page['newest']
            if page['newest'] < through:
                raise ObservationError(f'journal cursor regressed during snapshot: {page}')
            previous = after
            for record in page['records']:
                if (record['source']['component'] != component
                        or not after < record['sequence'] <= through):
                    raise ObservationError(f'invalid journal cursor/source: {record}')
                after = record['sequence']
                records.append(record)
            if page['next'] < after or page['next'] > through:
                raise ObservationError(f'invalid journal next cursor: {page}')
            if page['next'] == through:
                return records
            if page['next'] == previous:
                raise ObservationError(f'journal pagination stalled before snapshot end: {page}')
            after = page['next']
            if time.monotonic() >= deadline:
                raise TimeoutError(f'journal pagination did not finish for {component}')

    def wait(self, component, predicate, *, timeout=30):
        """Wait for log ingestion, not just process exit or the serial mirror."""
        deadline = time.monotonic() + timeout
        records = []
        while (remaining := deadline - time.monotonic()) > 0:
            records = self.snapshot(component, timeout=remaining)
            if predicate(records):
                return records
            self.shell.session.pump(min(0.1, max(0, deadline - time.monotonic())))
        raise AssertionError(f'{component}: expected journal records not ingested: {records!r}')

    def message(self, component, text, *, generation=1, stream=None, timeout=30):
        return self.wait(component, lambda records: any(
            record['source']['generation'] == generation
            and (stream is None or record['source']['stream'] == stream)
            and text in record['text'] for record in records), timeout=timeout)

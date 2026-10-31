# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Record all source owners, including exported workspaces without Git."""

import json
import os
from pathlib import Path
import subprocess
import sys


def source_inventory(workspace):
    source = Path(os.environ.get('HYPER_WORKSPACE', workspace)).resolve()
    output = Path(os.environ.get('HYPER_BUILD_OUTPUT', os.environ.get('OUT', source / 'out'))).resolve()
    path = subprocess.check_output(
        [sys.executable, str(source / 'project.py'), '--workspace', str(source),
         '--out', str(output), 'inventory'], cwd=source, text=True).strip()
    record = json.loads(Path(path).read_bytes())
    if record.get('version') != 1 or not record.get('identity') or not record.get('components'):
        raise ValueError('unsupported or incomplete workspace source inventory')
    components = {name: {key: owner[key] for key in ('commit', 'dirty', 'path')}
                  for name, owner in record['components'].items()}
    return {'identity': record['identity'],
            'dirty': any(owner['dirty'] for owner in components.values()),
            'components': components}

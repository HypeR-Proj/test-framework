# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hyper_test.provenance import source_inventory


class ProvenanceTests(unittest.TestCase):
    def workspace(self, root):
        root.mkdir()
        (root / 'project.py').write_text('''import argparse, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--workspace', type=Path)
parser.add_argument('--out', type=Path)
parser.add_argument('action', choices=['inventory'])
args = parser.parse_args()
assert args.workspace == Path.cwd()
path = args.out / 'state/source-inventory.json'
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text((args.workspace / 'fixture.json').read_text())
print(path)
''')
        record = {'version': 1, 'identity': 'snapshot', 'components': {
            'kernel': {'commit': 'kernel-revision', 'dirty': False, 'path': 'kernel', 'files': {'large': 'hash'}},
            'tests': {'commit': 'tests-revision', 'dirty': True, 'path': 'tests', 'files': {}}}}
        (root / 'fixture.json').write_text(json.dumps(record))
        return record

    def test_generated_view_uses_source_owner_and_external_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, view = root / 'source tree', root / 'external out', root / 'view'
            expected = self.workspace(source)
            with patch.dict(os.environ, {'HYPER_WORKSPACE': str(source),
                                         'HYPER_BUILD_OUTPUT': str(output)}, clear=True):
                record = source_inventory(view)
            self.assertEqual(record['identity'], expected['identity'])
            self.assertTrue(record['dirty'])
            self.assertEqual(record['components']['kernel']['commit'], 'kernel-revision')
            self.assertNotIn('files', record['components']['kernel'])
            self.assertTrue((output / 'state/source-inventory.json').is_file())
            self.assertFalse(view.exists())

    def test_gitless_workspace_and_rejected_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'archive'
            fixture = self.workspace(source)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(set(source_inventory(source)['components']), {'kernel', 'tests'})
                fixture['version'] = 2
                (source / 'fixture.json').write_text(json.dumps(fixture))
                with self.assertRaisesRegex(ValueError, 'inventory'):
                    source_inventory(source)


if __name__ == '__main__':
    unittest.main()

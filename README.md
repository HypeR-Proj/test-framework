<!-- SPDX-FileCopyrightText: 2026 roolrz -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# HypeR system test framework

This repository owns reusable host-side guest sessions, command transport,
journal parsing, timeout/report handling and the guest `test-report` protocol.
Test cases and fixture policy belong to
[system-tests](https://github.com/HypeR-Proj/system-tests); GitHub coordination
belongs to [ci](https://github.com/HypeR-Proj/ci).

From a synchronized source workspace:

```sh
python3 test-framework/runner.py list
python3 test-framework/runner.py --out /work/hyper-output fixture native --arch aarch64
python3 test-framework/runner.py --out /work/hyper-output run native-apps-aarch64
make test MODULE=test-framework
```

The runner discovers the registry owner from module metadata. `list` emits a
case and fixture matrix; GitHub Actions consumes the same matrix as local runs.
Every execution records a JSON result, elapsed time and log under OUT/results.
Timeouts terminate the process group, including descendant emulators.

Cases may declare `batch` as a name or an architecture-to-name map. Cases in a
batch share one worker and build output, execute sequentially, and must use the
same architecture and fixture. `list` emits one matrix row per batch, including
its complete `members` list. `run batch-NAME-ARCH` executes that row; individual
case IDs and case names remain available for local reproduction. Case failures
are recorded independently, later members still run, and any failure fails the
batch. Environments and process groups are isolated between members; only build
artifacts are reused. A batch must not rely on an earlier case running first.

The registry owner's `workspace.system_tests.diagnostics` may list glob patterns
using `{workspace}`, `{out}` and `{arch}`. Files must resolve inside OUT. Changed
diagnostics are copied after each case into unique directories beneath
`OUT/results/diagnostics/`, before another case can overwrite a shared log path.
Each case result lists its saved paths. CI must retain the complete results tree,
including command transcripts and failure records. Patterns should target runtime
output directories rather than traverse compiler caches.

Fixtures are built once per source/configuration/architecture identity. Consumers
verify identity and payload hashes before use, then create disposable disks as
needed. CI sets `CI_REQUIRE_FIXTURES=1` so a missing download fails instead of
silently rebuilding in each consumer job. A local run without a fixture may
build its inputs normally.

The Python API is provided by `python/hyper_test`; `make prepare` adds it to the
execution view's Python path. Guest protocol sources live under `guest/test-report`.
Framework self-tests run without a full emulator boot. System cases are explicit
manual acceptance work and never start automatically from a module PR.

Performance and hardware reports use `hyper_test.provenance.source_inventory` to
record the workspace identity and every component revision/dirty state. This also
works from generated execution directories and exported sources without Git.

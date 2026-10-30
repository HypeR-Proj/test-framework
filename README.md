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

Fixtures are built once per source/configuration/architecture identity. Consumers
verify identity and payload hashes before use, then create disposable disks as
needed. CI sets `CI_REQUIRE_FIXTURES=1` so a missing download fails instead of
silently rebuilding in each consumer job. A local run without a fixture may
build its inputs normally.

The Python API is provided by `python/hyper_test`; `make prepare` adds it to the
execution view's Python path. Guest protocol sources live under `guest/test-report`.
Framework self-tests run without a full emulator boot. System cases are explicit
manual acceptance work and never start automatically from a module PR.

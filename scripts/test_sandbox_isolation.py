#!/usr/bin/env python3
"""Phase-1 HARD GATE (ADR-035): the repo sandbox isolates secrets.

Sentinel secrets are planted in the PARENT environment, then the sandbox runs
a command that dumps its own environment and tries to read every secret var.
The assertion that gates the phase: NONE of the sentinel values is reachable
from inside the sandbox, and NONE appears in any captured output. The clone
step is stubbed (no network) so the RUN step exercises the REAL subprocess env
— the thing under test.

If this cannot pass, STOP: the code_worker and submit_pr rungs do not get
built on a sandbox that leaks credentials.

Run:
    python scripts/test_sandbox_isolation.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

# Sentinel secrets planted in the PARENT env. Every one is a credential the
# sandbox must never see (mirrors the public-safety sentinels + the new
# GITHUB_WRITE_TOKEN of Phase 3).
SENTINELS = {
    "ANTHROPIC_API_KEY": "sk-ant-SANDBOX-SENTINEL-aa11",
    "OPENAI_API_KEY": "sk-SANDBOX-SENTINEL-oo22",
    "LAB_OPERATOR_TOKEN": "tok-SANDBOX-SENTINEL-op33",
    "LAB_MCP_TOKEN": "mcp-SANDBOX-SENTINEL-mc44",
    "GITHUB_TOKEN": "ghp_SANDBOX_SENTINEL_gt55",
    "GITHUB_WRITE_TOKEN": "ghp_SANDBOX_SENTINEL_gw66",
    "DATABASE_URL": "postgres://u:pw-SANDBOX-SENTINEL-db77@h:5432/lab",
    "LAB_DATABASE_URL": "postgres://u:pw-SANDBOX-SENTINEL-ld88@h:5432/lab",
}

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILURES.append(msg)


# A command the sandbox runs: dump the child env AND try to read each secret
# var by name, on a marker line the parent greps. If isolation holds, every
# value is "<absent>" and no sentinel string appears.
_DUMP = (
    "import os, json; "
    "keys = " + json.dumps(sorted(SENTINELS)) + "; "
    "print('SANDBOX_ENV_KEYS=' + json.dumps(sorted(os.environ))); "
    "print('SANDBOX_SECRET_PROBE=' + "
    "json.dumps({k: os.environ.get(k, '<absent>') for k in keys}))"
)


def main() -> int:
    from lab_pack import repo_sandbox

    saved = {k: os.environ.get(k) for k in SENTINELS}
    os.environ.update(SENTINELS)

    # Stub the clone: create the repo dir without touching the network and
    # drop the probe script into it (avoids nested shell quoting). The RUN
    # step below is NOT stubbed — it is the real subprocess whose env we are
    # auditing.
    def fake_clone(repo, ref, dest):
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "probe.py"), "w") as f:
            f.write(_DUMP)
        return None

    repo_sandbox.set_clone_hook(fake_clone)
    try:
        # The lab's own repo is on the allowlist — the sandbox accepts it.
        result = repo_sandbox.run_repo_task(
            "yoheinakajima/activegraph-lab",
            f"{sys.executable} probe.py",
            timeout_seconds=30)
    finally:
        repo_sandbox.set_clone_hook(None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("== sandbox secret-isolation gate (ADR-035, Phase 1) ==")
    blob = json.dumps(result, default=str)

    base = result.get("baseline") or {}
    check(result.get("error") is None and base.get("exit_code") == 0,
          f"sandbox ran the probe command cleanly "
          f"(exit={base.get('exit_code')}, error={result.get('error')})")

    stdout = base.get("stdout") or ""
    # The child env: parse the dumped key list and the secret probe.
    child_keys: list[str] = []
    probe: dict[str, str] = {}
    for line in stdout.splitlines():
        if line.startswith("SANDBOX_ENV_KEYS="):
            child_keys = json.loads(line.split("=", 1)[1])
        elif line.startswith("SANDBOX_SECRET_PROBE="):
            probe = json.loads(line.split("=", 1)[1])

    check("PATH" in child_keys,
          "the sandbox env is functional (PATH present), not merely empty")
    leaked_keys = sorted(set(child_keys) & set(SENTINELS))
    check(not leaked_keys,
          f"no secret KEY is present in the sandbox env (leaked: {leaked_keys})")
    reachable = sorted(k for k, v in probe.items() if v != "<absent>")
    check(bool(probe) and not reachable,
          f"no secret VALUE is reachable inside the sandbox (reachable: {reachable})")

    # The whole result blob — captured stdout, stderr, every field — must
    # contain none of the sentinel values.
    present = sorted(name for name, val in SENTINELS.items() if val in blob)
    check(not present,
          f"no sentinel value appears in any captured output (present: {present})")

    # And clean_env() itself refuses to carry a secret, even if one were
    # mistakenly allowlisted.
    env = repo_sandbox.clean_env()
    check(not (repo_sandbox.SECRET_ENV_KEYS & set(env)),
          "clean_env() carries no secret key")
    check("PATH" in env, "clean_env() carries PATH")

    print(f"\ntest_sandbox_isolation: {'PASS' if not FAILURES else 'FAIL'} "
          f"({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

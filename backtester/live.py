"""Live modes on ``pre_tool_call``: shadow logs what the promoted policy would do, enforce returns it.

Hermes treats a ``pre_tool_call`` callback that raises or times out as a block, so shadow catches
everything and never returns a directive: a bug here must not stop a job the backtest is measuring.
Enforce fails closed when its policy can't load, as Hermes does for a policy hook that errors.

The hook reads its settings on every call (Hermes caches the config file by signature), so promoting
a policy takes effect on the next tool call. Files live in the profile's own ``backtest/`` folder:
the promoted policy copies, ``promotions.jsonl`` and ``decisions.jsonl``.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .evaluator import evaluate
from .ingest import _clean_args
from .policy import Policy, PolicyError, load_policy

MODES = ("off", "shadow", "enforce")
PROMOTIONS = "promotions.jsonl"
DECISIONS = "decisions.jsonl"
TAG = "policy-backtester"


def live_dir(home: Path) -> Path:
    return Path(home) / "backtest"


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def install_policy(home: Path, source: Path, policy: Policy) -> Path:
    """Copy a policy into the profile, named by its hash, so editing the source can't change what runs."""
    target = live_dir(home) / "policies" / f"{policy.hash[:12]}.yaml"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o600)
    return target


def record_promotion(home: Path, record: dict) -> None:
    _append(live_dir(home) / PROMOTIONS, {"ts": time.time(), **record})


def promotions(home: Path) -> list[dict]:
    return read_jsonl(live_dir(home) / PROMOTIONS)


def decisions(home: Path) -> list[dict]:
    return read_jsonl(live_dir(home) / DECISIONS)


class MemorySettings:
    """A profile's plugin settings held in memory: what the demo and the tests use in place of Hermes."""

    def __init__(self, home: Path, profile: str):
        self.values: dict[str, Any] = {}
        self._home, self._profile = Path(home), profile

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value

    def home(self) -> Path:
        return self._home

    def profile(self) -> str:
        return self._profile


class LiveGuard:
    """The ``pre_tool_call`` callback. Settings, home and profile are read through callables so the
    hook follows promotions without a reload and tests can run it without Hermes."""

    def __init__(self, setting: Callable[[str, Any], Any], home: Callable[[], Path], profile: Callable[[], str | None]):
        self._setting = setting
        self._home = home
        self._profile = profile
        self._lock = threading.Lock()
        self._cached: tuple[tuple, Policy] | None = None

    def __call__(self, tool_name: str = "", args: Any = None, task_id: str = "", session_id: str = "",
                 tool_call_id: str = "", **_: Any) -> dict | None:
        try:
            mode = self._setting("mode", "off")
        except Exception:
            return None
        if mode == "shadow":
            try:
                self._decide("shadow", tool_name, args, task_id, session_id, tool_call_id)
            except Exception:
                pass
            return None
        if mode != "enforce":
            return None
        try:
            verdict = self._decide("enforce", tool_name, args, task_id, session_id, tool_call_id)
        except Exception as exc:
            return {"action": "block", "message": f"[{TAG}] The enforced policy couldn't be checked ({exc}), so this call is blocked."}
        if verdict is None or verdict.action is None:
            return None
        rules = ", ".join(verdict.rule_ids)
        if verdict.action == "block":
            return {"action": "block", "message": f"{verdict.message} [{TAG}: {rules}]"}
        decisive = next(r for r in verdict.hits if r.action == "approve")
        return {"action": "approve", "message": f"{verdict.message} [{TAG}: {rules}]", "rule_key": f"{TAG}:{decisive.id}"}

    def _decide(self, mode: str, tool_name: str, args: Any, task_id: str, session_id: str, tool_call_id: str):
        policy = self._policy()
        args = args if isinstance(args, dict) else {}
        verdict = evaluate(policy, tool_name, args, self._profile())
        if verdict.hits:
            record = {
                "ts": time.time(), "mode": mode, "policy": policy.hash, "tool": tool_name, "action": verdict.action,
                "rules": list(verdict.rule_ids), "task_id": task_id or None, "session_id": session_id or None,
                "tool_call_id": tool_call_id or None, "args": json.loads(_clean_args(args)),
            }
            try:
                with self._lock:
                    _append(live_dir(self._home()) / DECISIONS, record)
            except Exception:
                if mode == "shadow":
                    raise
                # Enforce still returns its verdict when the log can't be written.
        return verdict

    def _policy(self) -> Policy:
        path = self._setting("policy", None)
        expected = self._setting("policy_sha256", None)
        if not path:
            raise PolicyError("no policy promoted")
        stat = Path(path).stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size, expected)
        if self._cached and self._cached[0] == key:
            return self._cached[1]
        policy = load_policy(Path(path))
        if expected and policy.hash != expected:
            raise PolicyError(f"{path} changed after it was promoted")
        self._cached = (key, policy)
        return policy

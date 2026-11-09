"""Live modes on ``pre_tool_call``: shadow logs what the promoted policy would do, enforce returns it.

Hermes treats a ``pre_tool_call`` callback that raises or times out as a block, so shadow catches
everything and never returns a directive: a bug here must not stop a job the backtest is measuring.
Enforce fails closed when its policy can't load, as Hermes does for a policy hook that errors. Either
way the call is logged with the reason, so ``log`` and ``status`` can say the policy isn't checking.
In both modes the built-in guard rules ride along (see ``guard.py``), so an agent can't switch it off.

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
from .guard import with_guard
from .ingest import _clean_args
from .paths import root_of, shown
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
            row = json.loads(line)
        except ValueError:
            continue
        # A hand-edited line can be valid JSON and still not a record.
        if isinstance(row, dict):
            rows.append(row)
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
            except Exception as exc:
                self._log_failure("shadow", None, exc, tool_name, args, task_id, session_id, tool_call_id)
            return None
        if mode != "enforce":
            return None
        try:
            verdict = self._decide("enforce", tool_name, args, task_id, session_id, tool_call_id)
        except Exception as exc:
            self._log_failure("enforce", "block", exc, tool_name, args, task_id, session_id, tool_call_id)
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
            # The record is built inside the try too: args a log can't hold mustn't turn enforce's verdict into a block.
            try:
                record = {
                    "ts": time.time(), "mode": mode, "policy": policy.hash, "tool": tool_name, "action": verdict.action,
                    "rules": list(verdict.rule_ids), "task_id": task_id or None, "session_id": session_id or None,
                    "tool_call_id": tool_call_id or None, "args": json.loads(_clean_args(args)),
                }
                with self._lock:
                    _append(live_dir(self._home()) / DECISIONS, record)
            except Exception:
                if mode == "shadow":
                    raise
                # Enforce still returns its verdict when the call can't be logged.
        return verdict

    def _log_failure(self, mode: str, action: str | None, exc: Exception, tool_name: str, args: Any, task_id: str,
                     session_id: str, tool_call_id: str) -> None:
        """Log a call the policy couldn't check: enforce blocked it, shadow let it through unchecked."""
        try:
            record = {
                "ts": time.time(), "mode": mode, "policy": self._setting("policy_sha256", None), "tool": tool_name,
                "action": action, "rules": [], "error": str(exc), "task_id": task_id or None,
                "session_id": session_id or None, "tool_call_id": tool_call_id or None, "args": json.loads(_clean_args(args)),
            }
            with self._lock:
                _append(live_dir(self._home()) / DECISIONS, record)
        except Exception:
            pass

    def _policy(self) -> Policy:
        path = self._setting("policy", None)
        expected = self._setting("policy_sha256", None)
        if not path:
            raise PolicyError("no policy promoted")
        try:
            stat = Path(path).stat()
        except OSError as exc:
            raise PolicyError(f"can't read policy {shown(path)}: {exc.strerror or exc}") from exc
        key = (str(path), stat.st_mtime_ns, stat.st_size, expected)
        if self._cached and self._cached[0] == key:
            return self._cached[1]
        policy = with_guard(check_promoted(Path(path), expected), root_of(self._home()))
        self._cached = (key, policy)
        return policy


def check_promoted(path: Path, expected: str | None) -> Policy:
    """Load a promoted policy copy, refusing one whose rules no longer hash to what was promoted, or one with no
    hash on record to check (a hand edit can blank the setting)."""
    if not expected:
        raise PolicyError(f"no hash is recorded for the promoted policy {shown(path)}, so it can't be checked")
    policy = load_policy(path)
    if policy.hash != expected:
        raise PolicyError(f"{shown(path)} changed after it was promoted: its rules hash to {policy.hash[:12]}, not {expected[:12]}")
    return policy

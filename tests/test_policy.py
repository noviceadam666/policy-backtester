from pathlib import Path

import pytest

from backtester.policy import PolicyError, load_policy, parse_policy

POLICIES = Path(__file__).resolve().parent.parent / "policies"
GOOD = {"id": "r", "tool": "terminal", "command": "x", "action": "block", "message": "m"}


def test_doc_policies_load_and_differ_only_by_two_lines():
    naive = (POLICIES / "naive.yaml").read_text().splitlines()
    refined = (POLICIES / "refined.yaml").read_text().splitlines()
    added = [line for line in refined if line not in naive and not line.startswith("#")]
    assert len(added) == 2
    assert all(line.strip().startswith("unless_command:") for line in added)
    assert load_policy(POLICIES / "naive.yaml").hash != load_policy(POLICIES / "refined.yaml").hash


def test_hash_ignores_comments_and_key_order():
    a = parse_policy({"rules": [GOOD]})
    b = parse_policy({"rules": [dict(reversed(list(GOOD.items())))]})
    assert a.hash == b.hash


@pytest.mark.parametrize(
    "rule,complaint",
    [
        ({**GOOD, "unless_comand": "x"}, "unknown field"),
        ({**GOOD, "command": "("}, "bad 'command' regex"),
        ({**GOOD, "unless_command": "["}, "bad 'unless_command' regex"),
        ({**GOOD, "action": "deny"}, "'action' must be"),
        ({k: v for k, v in GOOD.items() if k != "tool"}, "'tool' is required"),
        ({k: v for k, v in GOOD.items() if k != "message"}, "needs a 'message'"),
        ({k: v for k, v in GOOD.items() if k != "id"}, "needs a string 'id'"),
        ({**GOOD, "path": []}, "'path' must be"),
        ({**GOOD, "tool": 3}, "'tool' must be"),
        ({**GOOD, "path": ["**/.env"], "if_unresolved": "ask"}, "'if_unresolved' must be"),
        ({**GOOD, "if_unresolved": "approve"}, "only means something on a rule with a 'path'"),
        ({"id": "r", "tool": "terminal", "path": ["**/.env"], "action": "allow", "if_unresolved": "approve"},
         "'if_unresolved' needs a 'message'"),
        ({**GOOD, "args": ["to"]}, "'args' must map argument names to regexes"),
        ({**GOOD, "args": {}}, "'args' must map argument names to regexes"),
        ({**GOOD, "args": {"": "x"}}, "'args' must map argument names to regexes"),
        ({**GOOD, "args": {"to": "("}}, "bad 'args.to' regex"),
        ({**GOOD, "args": {"to": 3}}, "'args.to' must be a regex string"),
        ({**GOOD, "arg": {"to": "x"}}, "unknown field"),
    ],
)
def test_bad_rules_fail_loudly(rule, complaint):
    with pytest.raises(PolicyError, match=complaint):
        parse_policy({"rules": [rule]})


def test_bad_files_fail_loudly(tmp_path):
    with pytest.raises(PolicyError, match="can't read"):
        load_policy(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules: [")
    with pytest.raises(PolicyError, match="valid YAML"):
        load_policy(bad)
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text("rules:\n  - id: x\n    tool: terminal\n    tool: read_file\n    action: allow\n")
    with pytest.raises(PolicyError, match="valid YAML"):
        load_policy(duplicate)
    with pytest.raises(PolicyError, match="'rules' list"):
        parse_policy(["nope"])
    with pytest.raises(PolicyError, match="unknown top-level"):
        parse_policy({"rules": [], "version": 1})
    with pytest.raises(PolicyError, match="duplicate"):
        parse_policy({"rules": [GOOD, GOOD]})


def test_args_keep_their_order_and_the_shipped_tools_policy_loads():
    policy = parse_policy({"rules": [{**GOOD, "args": {"b": "1", "a": "2"}}]})
    assert [name for name, _ in policy.rules[0].args] == ["b", "a"]
    tools = load_policy(POLICIES / "tools.yaml")
    assert all(r.args for r in tools.rules) and all("terminal" not in r.tools for r in tools.rules)

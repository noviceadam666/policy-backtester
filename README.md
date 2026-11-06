# Policy Backtester

Backtest your Hermes guardrails against real history before you enforce them.

You wouldn't ship a trading strategy without a backtest. This plugin reads the tool calls your agents already
made, replays them through a policy, and tells you what the policy would have caught and which working jobs
it would have broken. Then it runs the policy in shadow on live traffic, and only then enforces it. It tests
rules; it doesn't replace Hermes's own approvals or other guardrails, and it never approves anything.

## Try it in a minute, no keys

```bash
hermes plugins install https://github.com/<owner>/policy-backtester
hermes plugins enable policy-backtester
hermes backtest demo
```

`demo` builds a small synthetic Hermes fleet in a temp folder, tells the whole story on it (backtest, fix,
shadow, enforce) through the same code the real commands use, and exits non-zero unless all 9 checks pass. It
doesn't read your history.

## On your own history

```bash
hermes backtest run policies/naive.yaml                       # what it would catch and break, last 30 days
hermes backtest diff policies/naive.yaml policies/refined.yaml
hermes -p ops backtest shadow policies/refined.yaml           # log decisions on live calls, change nothing
hermes -p ops backtest log                                    # what shadow would have stopped, jobs untouched
hermes -p ops backtest enforce                                # block or ask, from the next tool call
hermes -p ops backtest off
```

`run` and `diff` read every profile and update the index first unless you pass `--no-ingest`. Add `--profile
NAME` to limit a run, `--since 7d` or `--until 2026-11-06` to change the window, `--format json` for a report
and `--aggregate` for counts only. Output is redacted by default; `--no-redact` shows raw commands and titles.

### In CI

`--fail-on-break` makes `run` exit 3 when the policy would break a job, and `diff` exit 3 when the new version
breaks one the old didn't, so a policy change can fail review before it reaches an agent.

## Promotion: backtest, shadow, enforce

`shadow` and `enforce` act on the profile you run them in (`hermes -p <name>`), because Hermes runs plugin
hooks per profile. Both backtest the policy on that profile's history first and print the result.

- **shadow** starts even if the policy would break a job, since it changes nothing, and says so.
- **enforce** refuses unless that exact policy (by hash) has run in shadow on this profile and its backtest
  breaks no jobs. `--force` overrides, and the override is recorded.
- The policy is copied into `<profile>/backtest/policies/<hash>.yaml`, so editing your source file can't change
  what runs. The hook refuses a copy whose rules changed after promotion.
- Every promotion appends the policy hash and its backtest summary to `<profile>/backtest/promotions.jsonl`.

### What the live hook does

It's one `pre_tool_call` callback that reads the mode from this plugin's settings on every call, so a promotion
applies from the next tool call without a restart.

- **shadow** evaluates the call, appends any rule hit to `<profile>/backtest/decisions.jsonl`, and returns
  nothing. Hermes treats a callback that raises or times out as a block, so shadow catches every error itself:
  a bug here can't stop a job.
- **enforce** returns `block` with the rule's message, or `approve` to hand the call to Hermes's approval gate,
  which fails closed on denial or timeout. If the policy can't load, it blocks, as Hermes does for a policy hook
  that errors.
- **off**, the default, does nothing.

## What it reads, writes and sends

- **Reads** every profile's `state.db`, plus `cron/jobs.json`, `cron/executions.db` and Kanban board databases
  to tell whether a scheduled run completed. Every database is opened with SQLite `mode=ro` and never written.
- **Writes** its own index, `<hermes root>/backtest/index.db` (0600), and in each profile where you promote a
  policy: `backtest/policies/`, `promotions.jsonl`, `decisions.jsonl` (0600), and its three settings under
  `plugins.entries.policy-backtester.settings` (`mode`, `policy`, `policy_sha256`).
- **Sends** nothing. The plugin makes no network calls.

The index and the decision log keep each call's name, arguments and result status, plus session context. They
never keep result bodies, and file or patch bodies in arguments are replaced by a length. Commands are kept as
written, because policies match on them, so a secret typed into a command stays on your machine in those files.

## Policy format

```yaml
rules:
  - id: no-rm-rf
    tool: terminal              # a name, a list, or '*'
    profile: [demo-ops]         # optional
    command: 'rm\s+-rf'         # regex, searched in the whole command
    unless_command: '^\s*rm\s+-rf\s+\./cache/\*\s*$'
    path: ['**/.env']           # globs
    action: block               # block, approve or allow
    message: Recursive deletes are blocked.
```

All the matchers on a rule have to match. Unknown fields are errors, because an ignored `unless_comand` would
silently change what a rule does. Block beats approve beats no directive. `allow` means no objection, never
approval.

### `unless_command` works per segment, so anchor it

The evaluator splits a command at `;`, `&&`, `||`, `|`, `&`, newlines, parentheses and backticks, and
`unless_command` exempts only the segments it matches. `rm -rf ./cache/x && rm -rf /` still fires a rule
scoped to `./cache/`. The split ignores quoting on purpose: that catches `bash -c "a; b"`, and a split inside
a quoted string can only make a rule fire more.

A regex still only checks what you wrote, so anchor an exemption to the whole segment. The unanchored
`rm\s+-rf\s+\./cache/` also exempts `rm -rf ./cache/ /etc` and `rm -rf ./cache/../..`; the shipped
`policies/refined.yaml` anchors both of its exemptions to the exact commands its jobs run. A job that drifts
from them (say, adding `2>&1`) shows up in the backtest and the shadow log before enforce would stop it.

### `path` on a terminal call means "the command mentions it"

A terminal call has only a `command`, so a `path` glob matches any path the command mentions:

- option values and upload forms: `curl -F file=@config/vendor-api.yaml`, `-d@.env`, `--key=~/.ssh/id_rsa`
- redirects, substitutions and nested shells: `cat <.env`, `$(cat .env)`, `bash -c "cat .env"`
- relative paths resolved against `workdir` and any earlier `cd`
- `$HOME` and absolute home directories, for `~/` globs

It over-matches on purpose, because `echo .env >> .gitignore` is a mention too. The backtest is where you find
that out, and `unless_command` is how you scope it. It can't see through indirection: a shell glob
(`cat .en*`), a variable (`F=.env; cat $F`) or a parent directory (`tar c config/`).

## How to read the results

- **Caught** groups stopped calls by session. A call Hermes already denied still counts, marked `Hermes denied
  it`. An agent that retries counts once per session it tried in.
- **Would break** is a blocked call in a cron run or Kanban task that completed, grouped by job. An `approve`
  counts too, because nobody answers a prompt in an unattended run. A subagent counts toward the run that
  spawned it. A hit in a run that failed is an incident instead.
- Each call is replayed on its own. The replay can't know what the agent would have done after a block, so
  later counts in the same session are upper bounds.

## Limits

- Regex and glob rules see what a command says, not what it does. Treat a clean backtest as evidence, not proof.
- Enforcement runs per profile: install and promote in each profile you guard. One ingest still reads them all.
- Tested on Hermes v0.22.5 and v0.22.7.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest
```

`tests/test_hermes_e2e.py` runs through a local Hermes install's real plugin manager, its real
`pre_tool_call` dispatch and its validator, and skips without one. All test and demo data is synthetic; the
demo story lives in `fixtures/story.json`.

`hermes plugins validate` reports `caution`, not failure, on this repo: its policies, tests and fixtures
contain the very commands and paths it exists to catch.

## License

MIT. See [LICENSE](LICENSE).

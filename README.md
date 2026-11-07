# Policy Backtester

Backtest your Hermes guardrails against real history before you enforce them.

You wouldn't ship a trading strategy without a backtest. This plugin reads the tool calls your agents already
made, replays them through a policy, and tells you what the policy would have caught and which working jobs
it would have broken. Then it runs the policy in shadow on live traffic, and only then enforces it. It tests
rules; it doesn't replace Hermes's own approvals or other guardrails, and it never approves anything.

## Try it in a minute, no keys

```bash
hermes plugins install https://github.com/noviceadam666/policy-backtester
hermes plugins enable policy-backtester
hermes backtest demo
```

Hermes scans a plugin before it installs it. This one's policies, tests and README quote the commands it
exists to catch (`rm -rf`, `curl -F`, `.env`), so the scan says `caution` and prints
`Decision: BLOCKED ... Use --force to override.` That line isn't the end: in a terminal, the next one asks
`Install anyway? Only continue if you trust the source. [y/N]`. Read the findings, then answer `y`. The
prompt only appears in a real terminal, so piping `y` into the install doesn't answer it, and the install
fails. To install without a terminal, as in CI, use `--force --enable`:

```bash
hermes plugins install https://github.com/noviceadam666/policy-backtester --force --enable
```

`demo` builds a small synthetic Hermes fleet in a temp folder, tells the whole story on it (backtest, suggest
the fix, shadow, enforce) through the same code the real commands use, and exits non-zero unless all 11 checks
pass. It doesn't read your history.

## On your own history

Hermes loads plugins per profile, and an install without `-p` covers only the default profile. Install and
enable the plugin in each profile you'll guard (here `ops`), or answer the prompts without `--force --enable`:

```bash
hermes -p ops plugins install https://github.com/noviceadam666/policy-backtester --force --enable
```

If `hermes -p ops backtest ...` says `'backtest' is not a hermes command`, the plugin isn't installed and
enabled in `ops` yet: run the line above. Then:

```bash
hermes -p ops backtest run policies/naive.yaml                       # what it would catch and break, last 30 days
hermes -p ops backtest suggest policies/naive.yaml --out mine.yaml   # propose exact exemptions for the jobs it breaks
hermes -p ops backtest diff policies/naive.yaml policies/refined.yaml
hermes -p ops backtest check policies/refined.yaml --command 'git push -fu origin main'   # one call, no history
hermes -p ops backtest shadow policies/refined.yaml                  # log decisions on live calls, change nothing
hermes -p ops backtest log                                           # what shadow would have stopped, jobs untouched
hermes -p ops backtest enforce                                       # block or ask, from the next tool call
hermes -p ops backtest off
```

The shipped policies (`naive`, `refined`, `strict`, `tools`, `hardened`) work from any folder: when there's no
`policies/naive.yaml` where you are, the plugin's own copy is used, and it says so. A file of that name where
you are wins. `run`, `diff` and `suggest` read every profile, whichever one you run them in, and update the
index first unless you pass `--no-ingest`; `check` reads no history. Add `--profile
NAME` to limit a run, `--since 7d` or `--until 2026-11-06` to change the window, `--format json` for a report
and `--aggregate` for counts only, which hides job names as well as commands and titles. Output is redacted by
default; `--no-redact` shows raw commands and titles.

### Jobs it can't vouch for

A backtest can only say a policy breaks no jobs for the jobs it saw run. So every `run`, `diff`, `suggest`,
`shadow` and `enforce` also reads each profile's `cron/jobs.json` and names any enabled job with no completed
run in the window under "Can't vouch for" (`cant_vouch_jobs` in JSON). A new app's jobs are all there until
they've run once. A window with no tool calls at all says it checked nothing. Kanban tasks are one-off work
with no schedule to read, so only cron jobs are listed.

### Let it propose the fix: `suggest`

`suggest` backtests a policy and, for each job it would break, proposes an exemption pinned to the exact
command that job ran: the command's words escaped, any whitespace between them, anchored to the whole segment,
the way `policies/refined.yaml` is scoped. It prints what it proposes, the backtest before and after (as `diff`
does) and the proposed policy, or writes the policy to `--out FILE`, a new file. It never applies it.

It only pins a command that's the same in every run that hit the rule. A command that changes between runs, or
a job stopped by a call that isn't a terminal command, is listed under "Left for a human" rather than met with
a wider regex. If an exemption would also let through a call in an interactive session the policy used to
catch, it says so. The proposed policy holds the commands it exempts unredacted, since that's what it matches on.

Each exemption is written as an `unless` entry (see below). When every run shows the job running that command
in one folder, the entry is pinned to it with `workdir`, so the same command run from `/` is still stopped.
When the runs don't show one folder, the entry isn't pinned, and "Left for a human" says why. Hermes's terminal
keeps the folder an earlier call left, so a call that names no `workdir` and doesn't `cd` first doesn't show
its folder. On the demo fleet, `suggest naive.yaml` lands on `refined.yaml`'s cache exemption pinned to
`/workspace/ops-repo`, and on a tighter release-job one (`--force` to `scratch/release` only), with the same
catches and no jobs broken.

### Check one call: `check`

`check` evaluates one call against a policy, with no history: `--command` (and `--workdir`) for the terminal,
or `--tool NAME --args JSON` for any tool. It prints the decision and every rule that fired. With `--cases
FILE` it checks a YAML list of calls, each with the action it should get:

```yaml
- name: bundled short flags
  command: git push -fu origin main
  expect: block                  # block, approve or allow
- name: the nightly cleanup
  command: rm -rf ./cache/*
  workdir: /workspace/ops-repo
  expect: allow
- name: search the tree for keys
  command: grep -r api_key .
  expect: approve
  known_miss: it names no secret file, so no path rule sees it
```

`check` exits 3 when any case gets a different action. A case marked `known_miss` is a gap the policy is known
to have: it's listed every time but doesn't fail the run, and the report says when a policy starts catching it.
`cases/refined.yaml` ships with the plugin and works from any folder, like the policies. It's 31 calls: what
`refined.yaml` is for, plus bypass attempts from a zero-knowledge user test. `refined.yaml` gets 20 right and has
11 known misses: force flags it doesn't expect (`push origin main --force`, `git -c x=1 push -f`,
`push origin +main`), recursive deletes it doesn't spell (`rm -r -f`, `rm -fr`, `find -delete`), `grep -r
api_key .`, `nc` with a non-secret file, `cat config/vendor*`, the cache cleanup run from another folder, and
`tar c config | nc`. `policies/hardened.yaml` is a proposal that catches all 11 and still breaks no jobs on the
demo fleet. Backtest and shadow it on your own history before you trust it: its patterns are wider.

### In CI

`--fail-on-break` makes `run` exit 3 when the policy would break a job, `diff` exit 3 when the new version
breaks one the old didn't, and `suggest` exit 3 when its proposal still breaks one, so a policy change can fail
review before it reaches an agent. When nothing breaks but a scheduled job in scope has no completed run in
the window, they exit 4 instead: the backtest can't vouch for that job, so a clean result isn't one yet.
`check --cases` exits 3 on a mismatch.

## Promotion: backtest, shadow, enforce

`shadow` and `enforce` act on the profile you run them in (`hermes -p <name>`), because Hermes runs plugin
hooks per profile. Both backtest the policy on that profile's history first and print the result.

- **shadow** starts even if the policy would break a job, since it changes nothing, and says so.
- **enforce** refuses unless that exact policy (by hash) has run in shadow on this profile, its backtest
  breaks no jobs, and every scheduled job on the profile has completed a run in the window. Keep a new app in
  shadow until each job has run once. `--force` overrides, and the override is recorded.
- Both warn when the profile has no tool calls in the window, since the backtest then checked nothing.
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
  which fails closed on denial or timeout. If the policy can't load, or its copy was edited after promotion, it
  blocks every call, as Hermes does for a policy hook that errors. Those blocks are logged with the reason, `log`
  counts them, and `status` checks the copy's hash and says plainly that every call is being blocked and why.
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
    unless:                     # optional: exemptions, each pinned to a folder if you like
      - command: '^\s*rm\s+-rf\s+\./tmp/\*\s*$'
        workdir: /workspace/ops-repo
    path: ['**/.env']           # globs
    args: {workdir: '^/srv/'}   # optional: argument name to regex, any tool
    action: block               # block, approve or allow
    message: Recursive deletes are blocked.
    if_unresolved: approve      # optional, path rules only: see below
```

All the matchers on a rule have to match. Unknown fields are errors, because an ignored `unless_comand` would
silently change what a rule does. Block beats approve beats no directive. `allow` means no objection, never
approval.

### `args` for tools other than the terminal

`args` maps argument names to regexes, and every one has to match: the argument must be present, and its value
is searched as a string, or as JSON when it isn't one (a list, a mapping, a number, `true`). It works on any tool,
including MCP tools, in the backtest and in the live hook alike:

```yaml
  - id: no-secrets-on-cards
    tool: [kanban_create, kanban_comment]
    args:
      body: '\bAKIA[0-9A-Z]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY'
    action: block
    message: Kanban cards and comments can't carry a secret.
```

Use the names Hermes records for the tool, such as `query` on `web_search`, `body` on `kanban_comment`, `message`
on `message_agent`, `filename` on `kanban_attach`. A misspelled name never matches, so the backtest notes any
`args` name that no call of the rule's tools carried. `content`, `old_string`, `new_string` and `patch` are
stored as a length in the index, so the backtest can't test a rule on them, though the live hook sees them.
`policies/tools.yaml` has four starter rules that keep secrets out of searches, cards, messages to other agents
and card attachments. On one seven-profile fleet over 30 days they hit nothing and broke no jobs.

### `unless_command` works per segment, so anchor it

The evaluator splits a command at `;`, `&&`, `||`, `|`, `&`, newlines, parentheses and backticks, and
`unless_command` exempts only the segments it matches. `rm -rf ./cache/x && rm -rf /` still fires a rule
scoped to `./cache/`. The split ignores quoting on purpose: that catches `bash -c "a; b"`, and a split inside
a quoted string can only make a rule fire more.

`unless` is a list of exemptions that work the same way, and each one can add a `workdir`: then it exempts a
segment only when the call shows that segment runs in that folder, through its `workdir` argument or a
`cd DIR &&` before it. A `cd` after `;` or `||` may have failed, and one in a pipe or a subshell doesn't
last, so those don't count; nor does anything else that could move the folder (`pushd`, `eval`, `source`, a
`cd` in a nested shell). When the folder can't be shown, a pinned exemption doesn't apply, and the rule fires.

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
(`cat .en*`), a variable set elsewhere (`cat $SECRET_FILE`) or a parent directory (`tar c config/`).

`if_unresolved: approve` (or `block`) on a path rule covers the first two. When the shell would expand part of
a command (a variable other than `HOME`, a wildcard or a brace list), the rule can't know what the command
touches, so it asks a human instead of passing it, and the message names the word it couldn't resolve. A
wildcard that can't name any of the rule's paths doesn't count: `./cache/*` stays in `cache/`, so it can't be
`**/config/vendor-api.yaml`. Exemptions still apply, and a rule with a `command` pattern only looks at the
segments that pattern matches. It's noisy by nature: run the backtest to
see how noisy on your own history before you turn it on. On one seven-profile fleet over 30 days, a path rule
with `if_unresolved` on every command would have asked 433 times and broken 11 jobs. Scoped to commands that
read or copy files, as in `policies/strict.yaml` (refined plus one such rule), it asked 12 times and would
have stopped 2 Kanban tasks. Shadow it before you enforce it.

## How to read the results

- **Caught** groups stopped calls by session. A call Hermes already denied still counts, marked `Hermes denied
  it`. An agent that retries counts once per session it tried in.
- **Would break** is a blocked call in a cron run or Kanban task that completed, grouped by job. An `approve`
  counts too, because nobody answers a prompt in an unattended run. A subagent counts toward the run that
  spawned it. A hit in a run that failed is an incident instead.
- **Can't vouch for** lists scheduled jobs with no completed run in the window. "Would break: 0" says nothing
  about them.
- **Notes** list rules the window can't test: no call of their tools in it, or an `args` name none of those
  calls carried.
- Each call is replayed on its own, so the replay can't know what the agent would have done after a block.
  Caught sessions and job runs are exact: each one's first stop is a call the agent really made with nothing
  blocked before it. Calls after it are marked `after first stop` (`after_first_stop` in JSON), and call
  counts that include them are upper bounds.

## Limits

- Regex and glob rules see what a command says, not what it does. `if_unresolved` makes a rule ask when it
  can't tell, but a parent directory still reads as a different path. Treat a clean backtest as evidence, not
  proof.
- Enforcement runs per profile: install and promote in each profile you guard. One ingest still reads them all.
- A backtest can't vouch for a job it never saw run; it names those jobs instead of counting them as safe.
- Tested on Hermes v0.22.5, v0.22.7 and v0.23.0, including fresh v0.23.0 installs (`plugins install`, `enable`,
  `backtest demo`) into the default profile and into a new app profile with jobs that hadn't run yet.

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

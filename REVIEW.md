# Review instructions — agent-mgr

Repo-specific reviewer policy. The universal voice posture (Broken-Glass,
pro-simplification, and the don't-propose list) is supplied by the reviewers
themselves and is deliberately not restated here.

## Operating point

Pre-PMF, a single operator, four or five agents on one Linux host. Iteration
speed beats hardening for scale: prefer loud failures to fallbacks, pragmatic
DRY architecture to defensive layering, and don't guard edge cases that cannot
trigger at this size. A handled case the intent never asked for is a cost.

"Fail loudly" has deliberate exceptions here, and the axis that separates them
from bugs is **what the operator is left with** — not whether the command
exited 0, and not whether the suppression carries a comment.

A suppression is a **design decision** when the failure is surfaced and
classified: `lib/reload-if-running` prints which agent did not restart and the
command to run, `activate` prints "SUCCEEDED — do NOT re-run" because a red
exit would cost a second one-time activation, the relay probe splits `000`
from `401` because a dead network and a dead credential need different fixes.
Several `|| true` and `2>/dev/null` sites swallow a plain absence — a missing
registry, key or row the next line handles — and need no defence at all.

A suppression is a **finding** when it leaves the operator with no signal or a
wrong next step: a failed `compose ps` read as "no gateway running", so the
reload silently never happens and the caller reports success.

The pre-transition veto's contract is owned by `README.md` § What belongs in
an agent's repo. Do not restate it in a review — flag drift between it and
`require_transition_allowed` / `lib/reload-if-running`. The three rounds that
contract already drifted are why the tool owns it and the prose does not.

## Review priority

Subtractive remedies outrank additive ones. This repo is the **common** half of
a two-layer fleet — what is true of every Hermes agent — so the falsifiable
gate is what the tool *resolves or executes*: a hardcoded home, an agent's name
reaching a code path, a credential, or a recipe only one agent would ever run.

Three things are deliberately **not** violations of that gate, and flagging
them is a false finding: agent names used illustratively in comments, docs and
test fixtures; the Plow-platform commands this repo owns on purpose
(`check-latch`, `check-connectors`, the plugin and activation fetches), which
are specific to Plow rather than to any one agent; and a suggestion that would
make this tool *more* general than one operator's fleet needs, which is the
bloat rather than the fix.

The failure class this tool exists to prevent is **two gateways against one
home**. The image's s6 entrypoint starts a gateway whatever command you pass
it, so `docker compose run` without `--entrypoint` boots a second one that
answers alongside the live agent and posts a shutdown notice into the owners'
channel on exit. Measured before the fix: 25 gateway starts against a 1–6/day
baseline, 21 shutdown notices in one day, 6 sqlite errors from two gateways
racing one session database. Anything that reopens that path is blocking.

**Repo-specific contrast pairs:**

| Fleet-CLI DON'T (suppress / flag-as-shape) | Fleet-CLI DO (real finding) |
|---|---|
| Ask for a fallback when `docker`, `compose` or the relay fails. Refusing loudly is the design — a host-side answer is exactly the evidence entering the container's namespace was meant to stop accepting. | Flag an error that is **swallowed or misreported**: a failed `compose ps` read as "no gateway running", a dead credential and a dead network collapsed into one message, a probe that reports success without having run. |
| Demand a config-schema validator, a plugin abstraction, or a packaging story. This is one bash script symlinked onto one host, for one operator. | Flag a **new route to a container transition** that does not go through `compose_transition`, or a `compose run` that does not require `--entrypoint`. Both reopen the second-gateway class above. |
| Suggest guards for an agent count, host count or concurrency this fleet will not reach. | Flag a **write into an agent's home that skips `require_own_home`**. `resolve-guard` proves Compose agrees with the descriptor, which a descriptor copied from a sibling satisfies perfectly — it is self-consistent and wrong, and the write lands on the sibling. |
| Treat doc-only edits to `README.md` / `docs/HOWTO.md` as low-value churn. | Flag **prose↔code drift**. The README owns the agent-repo contract; a behaviour change that leaves its table describing the old behaviour is the canonical regression here, and it has recurred across rounds. |
| Propose vendoring an upstream artifact to avoid a fetch. | Flag any ref that is **not a 40-char SHA** — image digest, plugin, skill. A branch silently re-points a running agent on the next upstream push, and these carry the chat token and drive a filesystem. |
| — | Flag a **path that can traverse out of the agent's home**. Destinations are joined onto it and the result is `rm -rf`'d during a swap, so a traversing `--dest` is a delete primitive aimed at the operator's home. Reject by component, not substring — a legitimate `..foo` must still install. |
| — | Flag any **secret reaching argv, a URL, or a log line**. Credentials belong in the agent's dotenv, read through the home mount; compose passes none, and a token travels in a header rather than a path. |
| — | Flag an **agent-specific fact reaching a code path here**: a hardcoded `~/.hermes-<something>`, a PMS token, a Hostex or Seam call, a recipe only one agent would run. The test is whether a second agent would want it — not whether it mentions an agent, which comments and fixtures do freely. |

## Product context

**Stage:** Prototype, not shipped, no dates. One operator (srosro) running a
handful of Hermes agents — rentals, admin, house-hunting, and one belonging to
a different person — as containers on a single Linux host (`wakeup`).

**Distribution model:** A git clone in `~/services/agent-mgr` with the
`agent-mgr` script symlinked onto `PATH`. No release, no package, no versioning.
The registry at `~/.config/agent-mgr/agents` maps a name to an agent repo.

**What this repo is:** the **common** half of a two-layer fleet.

| layer | lives in | what it is |
|---|---|---|
| common | this repo | true of *every* Hermes agent — bring-up, activation, the pins, the veto seam |
| agent | `<agent>-hermes-agent` | everything else that agent needs: identity, config, its own skill, scripts, recipes |

There is no third repo per skill. A skill only one agent runs lives in that
agent's repo; a skill two agents share is pinned by SHA from wherever it lives
upstream, which is what `add-skill` is for. The dividing question is **would a
second agent want this**, not whether it is code or config.

`README.md` § What belongs in an agent's repo is the single owner of that
contract; findings about it are doc findings, and drift between it and the
resolver is a real one.

**Trust boundary (known and accepted):** an agent's dotenv sits on the host
side of the container mount, owned by the operator's uid. The container
boundary does not hide it from the operator, and the READMEs say so rather than
leaving it implied. Latch credentials decide which Mac an agent can drive; the
Mac authorises each action, so the approval surface stays on that machine.

**Update cadence:** edit when the stage changes — a second operator, a second
host, or a distribution story that is not a symlink.

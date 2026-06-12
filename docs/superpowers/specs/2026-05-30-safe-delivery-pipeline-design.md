# Safe Delivery Pipeline — Design

**Date:** 2026-05-30
**Status:** Approved (brainstorming → implementation)
**Author:** Claude (with MattFerzz)

## Problem

The repo is maintained by a non-developer with the help of coding agents. Two
pains motivated this work:

1. **A change broke the test suite, and "the agent should have run the tests"
   didn't prevent it.** Root cause was _not_ a skipped test — it was an
   **environment portability gap**: `userbot/recording.py` and
   `tests/test_recording.py` both `import audioop`, a stdlib module **removed in
   Python 3.13**. The maintainer's machine runs Python 3.14, so the import fails
   and a single collection error aborts the _entire_ pytest run. CI (matrix
   3.10–3.12) and the agent's environment (≤3.12) stayed green because `audioop`
   still exists there. The production server (Ubuntu 22.04, Python 3.10) is
   unaffected. So: "run tests locally" is meaningless if everyone runs a
   different Python, and CI never tested the version where it breaks.

2. **No continuous deployment.** Changes are applied to the live server by hand.
   Recon confirmed the server has drifted **42 commits behind** `origin/master`
   with a **dirty working tree** (~2,600 lines across 19 files). Those live edits
   were verified to be **stale duplicates already present in `origin/master`**
   (e.g. on-disk `geminiKeys.py` is byte-identical to the committed one), so
   nothing unique is at risk — but the drift proves the server is being developed
   on directly, which is fragile.

## Goals

- Make the test suite trustworthy and **portable across Python versions**.
- Give **any** AI agent (Claude, Gemini, Codex, Copilot) a hard, agent-agnostic
  gate that runs the tests before work is considered "done".
- **Continuously deploy** `master` to the Oracle server _only when CI is green_,
  so a broken change can never reach the live bot.
- Turn the server into a **pure deploy target** (no more live editing).

## Non-goals

- Migrating the delivery flow to PR-based (the friend keeps pushing to `master`;
  CI gates the _deploy_, not the merge).
- Deploying the separate `telegram-bot` service / `vapls-telegram-bot` repo.
- Reconfiguring or reusing the existing self-hosted `granja-luque` Actions
  runner (deploy uses SSH from a GitHub-hosted runner instead).
- Second-pass test coverage (`playCommand`, `apiServer`, `userbot`) — tracked
  separately.

## Verified environment facts (server recon, 2026-05-30)

| Area               | Fact                                                                                                                                           |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Host               | Oracle Ampere A1, Ubuntu 22.04.5 aarch64                                                                                                       |
| Python             | 3.10.12 in **both** venvs (`venv/`, `userbot/venv/`)                                                                                           |
| Services           | `discord-bot`, `indio-userbot`, `telegram-bot` — all `User=ubuntu`, `Restart=always`, system services; passwordless `sudo systemctl` confirmed |
| `discord-bot`      | `WorkingDirectory=~/vapls-discord-bot`, `ExecStart=venv/bin/python3 bot.py`                                                                    |
| `indio-userbot`    | `WorkingDirectory=~/vapls-discord-bot/userbot`, `ExecStart=userbot/venv/bin/python3 bot.py`, **own** `requirements.txt`                        |
| Repo               | `~/vapls-discord-bot`, remote = public `github.com/dilelu94/VaPls-Discord-Bot`, on `master`, 42 commits behind, dirty tree                     |
| `.env` files       | gitignored (main + userbot) — safe from `git reset --hard`                                                                                     |
| Untracked          | `.env*` backups, `audio_output/`, `downloads/`, and a `geminiKeys.py` byte-identical to upstream                                               |
| Self-hosted runner | present but registered to `dilelu94/granja-luque`, inactive — **not used**                                                                     |

## Design

Four stages. Stage 0 is a prerequisite hotfix; 1–3 are the pipeline.

### Stage 0 — Unblock the suite (portability hotfix)

Add the official stdlib backport so `audioop` imports on Python ≥3.13:

- `requirements-dev.txt`: add `audioop-lts; python_version >= "3.13"`
- `requirements.txt`: add `audioop-lts; python_version >= "3.13"` (future-proofs
  production if the server's Python is ever upgraded)

On Python ≤3.12 the marker is inert (stdlib `audioop` is used), so the server is
unaffected. After this, all 141 tests collect and run on Python 3.14.

### Stage 1 — Trustworthy CI

Widen the matrix in `.github/workflows/ci.yml`:

```
python-version: ["3.10", "3.11", "3.12", "3.13", "3.14"]
```

This makes CI exercise the versions where stdlib removals/portability breaks
appear — the exact class that caused the original failure. `fail-fast: false`
stays so one version's failure doesn't mask the others.

### Stage 2 — Local test-gate (agent-agnostic + Claude)

Canonical "definition of done" command, enforced three ways:

- **`Makefile`** with a `check` target → `pytest -q`. Single command everyone
  (humans + agents) runs. Include `install` (`pip install -r
requirements-dev.txt`) for convenience.
- **`.githooks/pre-push`** (committed, executable) runs `make check`; a red suite
  rejects the push. Activated once per clone with
  `git config core.hooksPath .githooks` — documented in `AGENTS.md` setup.
  Machine-enforced regardless of which agent drives git.
- **Claude Stop-hook** in `.claude/settings.json` (which is the symlink to
  `.agents/`, so it travels with the repo): runs the suite when Claude finishes,
  so Claude can't claim "done" on red. Claude-only by nature.
- **AGENTS.md "Definition of Done"** section: an explicit checklist every agent
  reads — run `make check`, all green, before claiming complete. Closes the loop
  for Gemini/Codex/Copilot, which the git hook covers at push time but which
  benefit from the in-loop instruction.

The git hook is the cross-agent enforcer; the Stop-hook tightens it for Claude;
AGENTS.md carries the intent to every agent in-loop. CI (Stage 1) + CD gating
(Stage 3) are the final backstop if a local gate is bypassed.

### Stage 3 — Continuous deployment (SSH from GitHub-hosted runner)

Add a `deploy` job to `.github/workflows/ci.yml`:

- `needs: test` — runs only if **every** matrix Python passed.
- `if: github.event_name == 'push' && github.ref == 'refs/heads/master'` — never
  on PRs or branches.
- Guard: if `SSH_HOST` secret is empty, the job **skips gracefully** (exit 0) so
  merging the pipeline before secrets are configured doesn't hard-fail.
- Connects over SSH using secrets `SSH_HOST`, `SSH_USER`, `SSH_KEY`, optional
  `SSH_PORT` (default 22), with a known-hosts step.
- Runs the server-side `scripts/deploy.sh`.

**`scripts/deploy.sh`** (idempotent, runs on the server):

1. `cd ~/vapls-discord-bot`
2. `git fetch origin`
3. Remove untracked files that collide with tracked upstream paths (the
   byte-identical `geminiKeys.py`), then `git reset --hard origin/master`.
   This reconciles the drift. It does **not** touch untracked `.env*`,
   `audio_output/`, `downloads/` (only a `git clean` would, which we do not run).
4. Smart dependency install:
   - if root `requirements.txt` changed in this pull → `venv/bin/pip install -r
requirements.txt`
   - if `userbot/requirements.txt` changed → `userbot/venv/bin/pip install -r
userbot/requirements.txt`
   - "changed" = diff between the pre-pull and post-pull commit for that path.
5. `sudo systemctl restart discord-bot indio-userbot` (not `telegram-bot`).
6. Verify both are `active` (`systemctl is-active`); exit non-zero (failing the
   deploy job loudly) if either did not come back.

**Operating principle:** after this lands, the server is a **pure deploy
target**. No editing files on the box — all changes go through `master` → CI →
deploy.

## Error handling

- A red suite on any Python version fails `test`, which blocks `deploy`.
- Empty `SSH_HOST` → deploy skips (no false failure pre-setup).
- `git reset --hard` is safe given verified-equivalent live edits; untracked
  secrets/data are preserved.
- A service that doesn't return to `active` fails the deploy job → visible in the
  Actions UI; `Restart=always` plus the previous code still running limit blast
  radius.

## Testing

- Stage 0/1 are validated by CI itself going green across 3.10–3.14.
- `make check` / `.githooks/pre-push` validated by running locally (suite green
  on Python 3.14 after Stage 0).
- Stage 3 `deploy.sh` is validated on first real deploy after secrets are set;
  the reconcile + restart + verify steps are observable in the Actions log and on
  the server (`systemctl is-active`, `git log -1`).

## Manual setup (outside the repo — requires the maintainer)

These are **not** done by the implementation; they need credentials/permissions:

1. Generate a dedicated deploy SSH keypair.
2. Append the **public** key to the server's `~ubuntu/.ssh/authorized_keys`
   (a change to the live server — do only with explicit approval).
3. Add GitHub repo **secrets**: `SSH_HOST` (141.148.84.55), `SSH_USER` (ubuntu),
   `SSH_KEY` (the private key), optional `SSH_PORT`.
4. One-time per clone, for the local hook: `git config core.hooksPath .githooks`.

## Rollout order

1. Stage 0 (unblock) + Stage 1 (matrix) + Stage 2 (gate) + Stage 3 (workflow +
   `deploy.sh`) land together on branch `ci/safe-delivery-pipeline`.
2. Push branch → CI runs the new matrix on the branch (deploy job is inert on
   non-master). Confirm green across 3.10–3.14.
3. Maintainer configures secrets + server key (manual steps above).
4. Merge to `master` → first CD run reconciles the drifted server and deploys.

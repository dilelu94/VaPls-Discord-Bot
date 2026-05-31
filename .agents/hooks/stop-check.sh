#!/usr/bin/env bash
# Claude Code "Stop" hook — block finishing a turn while the test suite is red.
#
# Wired up in .claude/settings.json (-> .agents/settings.json). Claude runs this
# when it is about to stop responding. If the branch has work (uncommitted
# changes, or commits not yet on origin/master) and `make check` fails, the hook
# returns decision:"block" so Claude keeps working until the suite is green.
#
# It deliberately does NOT run on pure-chat turns (clean tree, nothing ahead of
# origin/master) so asking a question doesn't trigger pytest.
#
# Cross-agent enforcement lives in .githooks/pre-push (blocks the push for ANY
# agent/human); this hook is the Claude-specific in-loop tightening.
set -uo pipefail

input="$(cat)"

# Avoid infinite loops: if we already blocked once for this stop, let it through.
case "$input" in
  *'"stop_hook_active":true'*|*'"stop_hook_active": true'*) exit 0 ;;
esac

# Move to the repo root.
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$root" ] || exit 0
cd "$root" || exit 0

# Only gate when this branch actually carries work:
#   - dirty working tree, OR
#   - local commits not present on origin/master
has_work=0
if ! git diff --quiet || ! git diff --cached --quiet; then
  has_work=1
elif [ -n "$(git log --oneline origin/master..HEAD 2>/dev/null)" ]; then
  has_work=1
fi
[ "$has_work" -eq 1 ] || exit 0

# Run the canonical check. Keep the log for the user/Claude to inspect.
log=/tmp/claude-stop-check.log
if make check >"$log" 2>&1; then
  exit 0
fi

# Tests are red — block the stop and tell Claude why.
printf '%s\n' '{"decision":"block","reason":"Test suite is RED. Run `make check`, read /tmp/claude-stop-check.log, and fix the failing tests before finishing. Per the Definition of Done in AGENTS.md, work is not complete while tests fail."}'
exit 0

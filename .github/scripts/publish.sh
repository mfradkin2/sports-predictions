#!/usr/bin/env bash
# Publish a refresh — and never publish output that was built by code main
# has since moved past.
#
# A refresh takes about five minutes. If a change to the site's code lands
# while one is running, that run's pages and payloads were produced by the
# older code. Pushing them, directly or replayed by a rebase, overwrites the
# new ones: the site then serves last version's pages against this version's
# data. That happened once and left the pages a revision behind until the
# following refresh. So before publishing, this asks whether main has gained
# any code, and if it has, rebuilds on top of it, carrying over the ledgers
# and model state this run produced.
set -uo pipefail

# The branch the site publishes from. A refresh started by a push to the
# refresh-tick branch (see update.yml) still builds and publishes main.
BRANCH="${SP_BRANCH:-${GITHUB_REF_NAME:-main}}"

# What the pipeline writes. Everything else in the tree is code — including
# assets/, which the pipeline only reads and hashes into each page, so it is
# deliberately not staged here.
GENERATED=(index.html mlb.html nhl.html nba.html nfl.html
           data history model_state
           mlb_2026_schedule_enriched.csv nhl_2026_schedule_enriched.csv
           nba_2026_schedule_enriched.csv nfl_2026_schedule_enriched.csv)
# The ledgers and learned state: what a rebuild has to carry across.
STATE=(data history model_state)
# Everything the pipeline writes, excluded — so what is left is the code.
NOT_CODE=(':(exclude)data' ':(exclude)history' ':(exclude)model_state'
          ':(exclude)*.html' ':(exclude)*_schedule_enriched.csv')

die() {
  echo "Publishing nothing: $1." >&2
  exit 1
}

git config user.name  "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

# The commit whose code produced what is in the working tree. It moves only
# when the pipeline is actually re-run, never merely because we rebased.
built_from="$(git rev-parse HEAD)"

rebuild_if_code_moved() {
  git fetch -q origin "$BRANCH" || return 1
  local head
  head="$(git rev-parse "origin/$BRANCH")" || return 1
  [ "$head" = "$built_from" ] && return 1
  if git diff --quiet "$built_from" "$head" -- . "${NOT_CODE[@]}"; then
    return 1                      # only data moved; the resync below covers it
  fi
  echo "::notice::main gained code while this refresh was running; rebuilding on it"
  # Past this point the rebuild is not optional. Falling back to publishing
  # would push pages built by code main has moved past, which is the whole
  # reason this function exists; a skipped refresh costs fifteen minutes and
  # the next one starts clean. So a failure here stops the script.
  local keep present=() path
  keep="$(mktemp -d)" || die "could not make room to preserve this run's ledgers"
  for path in "${STATE[@]}"; do
    [ -e "$path" ] && present+=("$path")
  done
  if [ ${#present[@]} -gt 0 ]; then
    cp -r "${present[@]}" "$keep"/ || die "could not preserve this run's ledgers"
  fi
  git reset --hard "$head" || die "could not move onto the new code"
  rm -rf "${STATE[@]}"
  cp -r "$keep"/. . || die "could not restore this run's ledgers onto the new code"
  rm -rf "$keep"
  built_from="$head"
  # shellcheck disable=SC2086
  python3 run_pipeline.py ${SP_PIPELINE_ARGS:-} \
    || die "the rebuild on the new code failed"
}

# Move onto whatever origin has now, without rebasing.
#
# Two refreshes overlap often enough that this is the normal case. A rebase
# replays this run's commit over the other's and conflicts on every generated
# file they both wrote; the conflict leaves HEAD detached with a half-finished
# rebase on disk, and every later attempt dies on "not currently on a branch".
# That is what once burned four attempts and published nothing. There is
# nothing to resolve anyway: the pages and payloads this run built are the
# fresher ones and simply win. Only the ledgers need care — they are written
# once and never rewritten, so a row only the other run has is a row this run
# cannot reproduce. Those are folded in; everything else stays as built.
resync_onto_origin() {
  git rebase --abort >/dev/null 2>&1
  git merge --abort  >/dev/null 2>&1
  rm -rf .git/rebase-merge .git/rebase-apply
  git symbolic-ref -q HEAD >/dev/null || git checkout -q -B "$BRANCH"
  git fetch -q origin "$BRANCH" || return 1
  local theirs
  theirs="$(mktemp -d)" || return 1
  if git archive "origin/$BRANCH" history 2>/dev/null | tar -x -C "$theirs"; then
    python3 scripts/merge_history.py "$theirs/history" history || true
  fi
  rm -rf "$theirs"
  # Keep every file as this run left it; only move the branch under them, so
  # what gets staged next is this run's output on top of origin.
  git reset -q --mixed "origin/$BRANCH"
}

# Stage what the pipeline wrote. A path that is not there — a league whose
# page has never been built — makes git add abort and stage nothing at all,
# which then reads exactly like "nothing changed" and publishes nothing. So
# only paths that exist, here or in the last commit, are named.
stage_generated() {
  local present=() path
  for path in "${GENERATED[@]}"; do
    if [ -e "$path" ] || git cat-file -e "HEAD:$path" 2>/dev/null; then
      present+=("$path")
    fi
  done
  [ ${#present[@]} -gt 0 ] || return 0
  git add -A -- "${present[@]}"
}

for attempt in 1 2 3 4; do
  rebuild_if_code_moved
  stage_generated
  if git diff --cached --quiet; then
    # Nothing new to stage. That is only "nothing to do" when there is also
    # nothing already committed and unpushed — after a failed push, this
    # run's commit can be sitting right here waiting to go.
    if [ "$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)" -eq 0 ]; then
      echo "Nothing changed this run."
      exit 0
    fi
    echo "Nothing further to stage; pushing the commit from the previous attempt."
  else
    git commit -q -m "Auto-refresh: predictions and player props $(date -u +'%Y-%m-%d %H:%M UTC')"
  fi
  if git push; then
    echo "Pushed on attempt $attempt."
    exit 0
  fi
  echo "Push failed (attempt $attempt); syncing with origin."
  resync_onto_origin || echo "Could not sync with origin; trying the push again anyway."
  sleep $((2 ** attempt))
done

echo "Could not push after 4 attempts." >&2
exit 1

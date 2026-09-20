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

BRANCH="${GITHUB_REF_NAME:-main}"

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
    return 1                      # only data moved; a plain rebase covers it
  fi
  echo "::notice::main gained code while this refresh was running; rebuilding on it"
  local keep
  keep="$(mktemp -d)" || return 1
  cp -r "${STATE[@]}" "$keep"/ || return 1
  git reset --hard "$head" || return 1
  rm -rf "${STATE[@]}"
  cp -r "$keep"/. . || return 1
  rm -rf "$keep"
  built_from="$head"
  # shellcheck disable=SC2086
  python3 run_pipeline.py ${SP_PIPELINE_ARGS:-}
}

for attempt in 1 2 3 4; do
  rebuild_if_code_moved
  git add -A -- "${GENERATED[@]}"
  if git diff --cached --quiet; then
    # Nothing new to stage. That is only "nothing to do" when there is also
    # nothing already committed and unpushed — after a rebase on a failed
    # push, this run's commit is sitting right here waiting to go.
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
  git pull --rebase --autostash origin "$BRANCH" || true
  sleep $((2 ** attempt))
done

echo "Could not push after 4 attempts." >&2
exit 1

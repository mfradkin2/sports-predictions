# Sports Predictions

A four-sport prediction site (MLB, NFL, NBA, NHL) that rebuilds itself four
times an hour from ESPN and The Odds API and publishes to GitHub Pages from
`main`. Pure standard-library Python; no dependencies to install.

## Read first

- **`README.md`** — how the model works and how it improves itself.
- **`docs/RUNBOOK.md`** — how the site is supposed to run: the five promises
  it makes to a reader, the moving parts, the instruments, the standing
  conditions that look like faults but are not, and every failure mode that
  has actually happened with its cause and its fix. **Read this before
  diagnosing anything.**

## Checks

Run these before landing a change. They answer different questions.

```bash
python3 -m unittest discover -s tests   # does the code still do what it should
python3 scripts/health_check.py         # is the published site current and honest
python3 run_pipeline.py <league>        # can the site still rebuild itself
python3 scripts/check_pages.py          # do the pages render and navigate
```

`git fetch origin main && git checkout -B main origin/main` first. Every
check reads the working tree, so a stale checkout reports a stale site.

ESPN and The Odds API are unreachable from the sandbox, so add
`--no-ingest --no-props` to a local pipeline run and judge live behaviour
through GitHub Actions.

## Rules

- `history/` is an append-and-grade-only audit trail, and `model_state/` is
  learned state. Never hand-edit either; never rewrite a locked pick or a
  graded row.
- Never delete, skip or weaken a test or a check in `sportspred/verify.py`
  to make something pass. If a guard is wrong, correct it deliberately and
  add a test for the case it got wrong.
- `ODDS_API_KEY` lives only as a repository secret. Never print, move or
  commit it.
- Never put AI model names or identifiers in code, comments, commit messages
  or documentation.
- Keep the site's plain-English tone. Explain what a number means, not just
  what it is.
- When reporting to the owner, state times in US Eastern, not UTC. Commit
  timestamps, GitHub Actions times and the payload's `generated` field are
  all UTC, so convert them. Write "ET" and use whichever offset is actually
  in effect on the day — EDT is UTC-4 from the second Sunday in March to the
  first Sunday in November, EST is UTC-5 otherwise — rather than assuming
  one: `datetime.now(timezone.utc).astimezone(ZoneInfo('America/New_York'))`.
  Code, commit messages, log output and the data itself stay in UTC; this is
  only about what a person reads.

## Agents

- **`site-doctor`** (`.claude/agents/site-doctor.md`) — breakage only:
  diagnoses and fixes technical problems with the site. Runs every three
  hours, and can be invoked directly whenever something looks wrong. The
  gap between visits is deliberate: a bad build is already blocked from
  publishing by the gate in `sportspred/verify.py`, and the refresh retries
  itself every fifteen minutes, so the agent is there for what those two
  cannot fix by themselves.
- A separate daily routine owns prediction accuracy and proposes one
  evidence-backed model improvement at a time on its own branch.

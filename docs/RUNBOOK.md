# Runbook — how the site is supposed to run, and what to do when it doesn't

This is the operating manual for the Sports Predictions site. It exists so
that whoever is on call — a person or an agent — can tell in a few minutes
whether the site is healthy, and if it is not, fix the right thing.

Read the first two sections before touching anything. They are what
"running properly" actually means here; everything after is diagnosis.

---

## 1. The promises

The site makes five promises to a reader. Every incident worth the name is
one of them being broken, and nothing else is an emergency.

1. **The board is current.** Games, picks and prop lines reflect the last
   refresh, which runs four times an hour.
2. **A pick never changes after kickoff.** Whatever was published before the
   game started is what gets graded, win or lose. This is the promise that
   cannot be repaired after the fact — a pick quietly rewritten post-hoc
   makes the entire record worthless, so it outranks everything else here.
3. **Every finished game and settled prop shows up in Results,** with the
   verdict it earned.
4. **The record is honest.** What the site claims it got right is what it
   actually got right: no dropped losses, no double-counted wins, no picks
   that quietly never got scored.
5. **The page works.** It renders, on a phone and on a desktop, without
   throwing, without `undefined` where a number belongs.

## 2. The moving parts

```
ESPN + The Odds API  ->  run_pipeline.py  ->  sportspred/*  ->  data/, history/, *.html
                                                  |
                              .github/workflows/update.yml  (4x an hour, :02 :17 :32 :47)
                                                  |
                              .github/scripts/publish.sh  ->  git push main  ->  GitHub Pages
```

- **`run_pipeline.py`** ingests, trains, predicts, prices props, renders. Pure
  standard library. One league failing does not stop the others.
- **`sportspred/verify.py`** runs at the end of every build and reads the
  output back. If it objects, the build exits non-zero and **nothing is
  published** — a bad build costs one skipped refresh, which is the
  intended behaviour, not a failure.
- **`.github/scripts/publish.sh`** commits and pushes. It rebuilds if `main`
  gained code mid-run, and merges the ledgers if another refresh pushed
  first.
- **`history/`** is the audit trail: the ledgers, the frozen boards. It is
  append-and-grade-only. **Never hand-edit it.**
- **`model_state/`** is learned state. Regenerable, but do not hand-edit it
  either.
- **`data/`**, the `*.html` pages and the enriched CSVs are pure output.
  Regenerable from the ledgers and the feeds.

## 3. The instruments

Run these in this order. Each answers a different question.

| Command | Question it answers |
| --- | --- |
| `python3 scripts/health_check.py` | Is what readers see right now correct and current? |
| `python3 -m unittest discover -s tests` | Does the code still do what it is supposed to? |
| `python3 run_pipeline.py <lg>` | Can the site rebuild itself from the feeds? |
| `python3 scripts/check_pages.py` | Do the pages render and navigate properly in a browser? |

**Before running any of them, make the checkout match what is published:**

```bash
git fetch origin main && git checkout -B main origin/main
```

This is not optional. Nearly every check reads the working tree, so a stale
checkout reports a stale site: old boards, mismatched asset hashes, frozen
boards that look thawed. `health_check.py` says so explicitly when the
checkout is behind, and that finding invalidates everything under it.

## 4. Known standing conditions — not faults

Do not "fix" these. They are understood, and chasing them wastes a call-out.

- **`props_status` is not `live`,** or prop lines say `model` rather than
  `book`. Out-of-season leagues and games outside the 36-hour pricing window
  legitimately have no sportsbook lines; the board falls back to lines
  derived from season baselines and says so on the page.
- **MLB stolen bases (`sb`) are retired.** The ESPN box score carries no
  stolen-base column, so the market could be priced but never scored:
  roughly 900 picks reached the ledger and none of them ever got a verdict.
  It is no longer published or priced. The old picks stay in the ledger,
  because that is an audit trail and they really were published, and the
  Batting chip still lists the market so those picks keep their category on
  past boards. The health check ignores markets that are no longer
  published, so none of this shows up as a fault.
- **A prop row with `played=0` has no verdict.** The player did not take the
  field. Ordinary.
- **A prop row marked void** came from a corrupt feed day (a hitter at
  sixteen hits). It stays in the ledger as published but counts nowhere.
- **NBA and NHL have few or no graded games** out of season.
- **Gaps of up to about 50 minutes between published commits** are normal:
  runs that change nothing exit without pushing, and overlapping runs are
  cancelled by the workflow's concurrency group.

## 5. Failure modes

Each of these has actually happened, or is the thing a guard was built to
stop. Signature first, because that is what you will see.

### The board is hours old / nothing has been published
**Check:** the workflow's recent runs. A failing run is a build failure; a
succeeding run that pushes nothing is a publish failure.
**Common causes:** the build gate rejected the output (look for `Publishing
nothing:` in the log — then find *what* it objected to, and fix that, not the
gate); ESPN unreachable; the publish step failing.
**Fix:** address the underlying objection and let the next scheduled refresh
publish, or trigger `update.yml` once the fix is on `main`.

### A refresh failed on the publish step
**Signature:** the build succeeded, `Publish` is red.
**History:** two refreshes overlapped, the second rebased over the first,
every generated file conflicted, and the checkout ended up detached
mid-rebase — all four retries then died on "not currently on a branch".
Publishing no longer rebases; it merges the ledgers instead
(`scripts/merge_history.py`).
**Fix:** read the log. If it is a new race, reproduce it against a throwaway
repository before changing `publish.sh` — this script is load-bearing and
easy to make worse.

### The pages are a revision behind the data
**Signature:** `health_check.py` reports `asks for app.js?v=... but the assets
in this build hash to ...`.
**Cause:** pages rendered by code that `main` has since moved past.
**Fix:** confirm the checkout is current first — this is the most common
false positive. If it is genuinely on `main`, trigger a refresh; the build
gate should already have caught it.

### Finished games or props are missing from Results
**Check:** `health_check.py` reports graded ledger rows that cannot be
reached. If it does not, the data is fine and the fault is in the page.
**History:** the Results tab remembered the Track record rail, which lists no
games at all, so every later visit showed an empty section. Fixed by
`resetSection` in `assets/app.js`; `scripts/check_pages.py` now checks that
each tab lands on its own front door.

### A market is published but never scored
**Signature:** `health_check.py` reports `none of the last 10 <market> picks
were scored`.
**Why it matters:** silent. The picks look right on the page and simply never
arrive in the record.
**History:** NFL pass attempts. The box score gives completions and attempts
in one cell as "20/31" and only the first number was read, so every pass-
attempts pick went ungraded. Fixed by `BOX_PAIRS` in `sportspred/espn.py`.
**Fix:** find where the stat is read from the box score. If the feed does
carry it, map it; if it genuinely does not, say so and let the owner decide
whether to keep publishing the market.
**Note:** the check looks at the most recent handful, not all time, so it
lights up within about one slate of a market breaking and goes out as soon
as a fix actually grades something. Picks that went unscored before the fix
stay unscored — a graded row is never rewritten — so the record keeps that
gap permanently, and the alarm correctly stops nagging about it.

### A pick appears to have changed after kickoff
**Treat this as the most serious thing in this document.**
**Check:** `health_check.py` compares each game's start time against the
ledger's `predicted_at`.
**Fix:** never by editing the ledger. Find what let the pick be rewritten —
the locking path in `sportspred/learn.py` and the frozen boards — and fix
that. The bad rows stay as published; the record is an audit trail, not a
scoreboard to be tidied.

### Every run logs `team statistics for 0/N teams`
**Signature:** the ingest step of every league, every run, reports statistics
for no team at all. The build still succeeds, so nothing goes red.
**Why it matters:** silent. MLB's standings prior goes without ERA, NFL
without yardage and turnovers, NHL without save percentage, and the daily
`history/<league>_team_stats.csv` snapshot records blanks for those columns.
**History:** ESPN serves a team's season line under
`results.stats.categories`; the parser looked under `splits.categories`, the
shape it was first written against, and found nothing from the day of the
Python port. Fixed in `_pick_stat` in `sportspred/ingest.py`, which now reads
either shape; the fixtures in `tests/espn_fixtures.py` carry the real one.
**Fix:** fetch one team's `/statistics` payload and look at where the
categories actually live before touching the parser. Add the new shape to
the fixtures.

### The page never shows a live score, and finished games wait for the next rebuild
**Signature:** the browser console shows ESPN scoreboard requests failing
with a 400 (or reported as a CORS error, which is what a browser says about a
failed cross-origin response). The board itself is fine, because the live
layer falls back to the static build.
**History:** the page asked for yesterday and today in one request as
`?dates=YYYYMMDD-YYYYMMDD`. ESPN stopped accepting the range and answers 400
on both hosts, for every league. `fetchScoreboard` in `assets/app.js` now
asks for each day on its own and merges the events.
**Fix:** `scripts/check_pages.py` runs without a network, so it cannot see
this. Load a page in a real browser and watch the console; every request the
live layer makes should come back 200.

### Props on a finished game sit under "In progress" for days
**Signature:** the props board's In progress rail holds hundreds of rows
while nothing is being played.
**History:** the stolen-base market was retired (see section 4) after
roughly nine hundred picks reached the ledger without a verdict. The rows
stayed on the boards frozen for those games, as intended, but the page's
status rule knew only open, in progress and graded, so a locked pick without
a verdict read as in progress for ever. A prop on a finished game with no
verdict is now `unscored`: it appears under All and on its game's board,
marked NOT SCORED, and counts nowhere else.
**Fix:** if it recurs with a market that is still published, that is the
"published but never scored" case above, not this one.

### The build gate is rejecting a good build
Possible, and the reason `skip_verify` exists as a workflow input. Be very
sure: the gate has been right every time so far. If it really is wrong, fix
the check and add a test for the case it got wrong, rather than leaving the
gate bypassed.

## 6. Fixing something safely

1. Reproduce it. A finding you cannot reproduce is a finding you cannot
   verify you fixed.
2. Work on a branch off current `main`.
3. Make the smallest change that addresses the cause, not the symptom.
4. Add a test that fails before your change and passes after. Every failure
   mode above earned one.
5. Run all four instruments in section 3. They must all pass.
6. Merge to `main` and push. The next refresh publishes it; trigger
   `update.yml` if it should not wait.
7. Confirm afterwards that the refresh went green and `health_check.py` is
   clean against the published tree.

## 7. Never

- Never edit `history/` or `model_state/` by hand, and never rewrite a locked
  pick or a graded row.
- Never delete, skip or weaken a test, or a check in `verify.py`, to make a
  build pass. If a guard is wrong, correct it deliberately and say so.
- Never force-push, and never rewrite published history.
- Never commit a secret. `ODDS_API_KEY` lives only as a repository secret;
  do not print it, move it, or echo it into a log.
- Never put AI model names or identifiers in code, comments, commit messages
  or documentation.
- Never widen the job while fixing an incident. Note what else you found and
  leave it for the daily optimiser review or the owner.

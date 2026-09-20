# ghost-watcher

Watches a private GitHub repo and captures it the instant it becomes public —
running entirely on GitHub's free infrastructure. No PC, no VPS.

Target repo is set by the `TARGET_REPO` variable (defaults to
`mrhakash/ghost-ai-writer-v2`).

## Setup (about 3 minutes)

1. Create a **new public repo** on GitHub — e.g. `ghost-watcher`.
   It must be **public**: public repos get unlimited free Actions minutes.
   Private repos are capped at 2,000 min/month, which this would burn in a day.

2. Upload these three files, keeping the folder structure:
   ```
   ghost_watch.py
   .github/workflows/watch.yml
   .github/workflows/keepalive.yml
   ```
   Use the web UI: **Add file → Upload files**. Drag them in, commit.

3. **Settings → Actions → General →** confirm Actions are allowed
   (they are by default on a fresh repo).

4. To watch a different repo: **Settings → Secrets and variables → Actions →
   Variables → New variable**, name `TARGET_REPO`, value `owner/name`.

5. Start the first watcher immediately, rather than waiting for the hour:
   **Actions → watch-repo → Run workflow**.

That's it. From here it runs itself.

## How the coverage works

A watcher job starts every hour and polls for 5h50m before exiting, so roughly
**six jobs overlap at any given moment**. That redundancy is deliberate:
GitHub's scheduled triggers are routinely delayed by 10–30 minutes and are
sometimes dropped entirely during peak load. One cron job on its own would
leave gaps big enough to miss a 40-second window; six staggered ones don't.

Polling is every 3 seconds. That's a deliberate ceiling, not a limitation of
the script — GitHub rate-limits (HTTP 403) aggressive polling from datacenter
IP ranges, which is exactly what Actions runners are. This was measured, not
guessed: 1-second polling from a cloud host got throttled within 96 requests.
The script backs off exponentially when throttled and resumes automatically.

Worst-case detection lag is therefore ~3 seconds, and capture puts the first
bytes on disk ~0.3s after that. Well inside a 40-second window.

## When it fires

All capture methods run in parallel, not in sequence:

- `git clone --mirror` — full history, every branch and tag
- API tarball + zipball of the default branch
- codeload `tar.gz` for `main` and `master` — fallback if the API is throttled
- full metadata: repo JSON, branches, tags, releases, commits, issues, PRs, contributors
- a normal working checkout, built offline from the mirror
- any release assets

Then:

- the capture is uploaded as a **workflow artifact** (kept 90 days)
- an **issue is opened in your watcher repo**, which GitHub emails you about

Grab the artifact from **Actions → the run that fired → Artifacts**.

## Optional: a PAT for higher API limits

The detection poll needs no auth. The metadata/tarball capture goes through the
GitHub API, which is 60 req/hr unauthenticated — enough, but tight.

Add a fine-grained PAT with **public read** scope as a secret named `GH_PAT`
(**Settings → Secrets and variables → Actions → New repository secret**) and
the capture gets 5,000 req/hr instead. The script never logs the token.

## Honest limitations

- **A 404 can't be told apart from a rename.** If the repo is renamed before it
  goes public, the watcher waits on a path that will never resolve. Watching the
  whole account's repo list instead is the fix if you think that's likely.
- **Scheduled workflows get disabled after 60 days** of repo inactivity.
  `keepalive.yml` pushes a weekly timestamp commit to prevent that.
- **Free-tier concurrency is 20 jobs.** Six overlapping watchers fits, but don't
  add other heavy workflows to this same repo.
- **Deleted, not just private.** If the repo never existed at that exact path, or
  was deleted outright, this waits forever. Worth confirming the path is right.

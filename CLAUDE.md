Never commit secrets or keys into the repository

Inventory lives in `./repo-map.md`. Domain vocabulary lives in `./CONTEXT.md` — use its
terms (Proxy, Ingress, Profile, Profile prefix, Worker, Relay, Seam, Default branch) in
code, docs, and commits. Read `docs/CLAUDE.md` for ADR routing before changing anything
ADR-governed.

Hook gotcha: this repo's PreToolUse hook blocks all file reads/writes until
`docs/CLAUDE.md` has been successfully read in the session, and re-arms after that file
is edited (re-read it before the next write). `docs/CLAUDE.md` must always remain a
readable FILE — if it is missing or a directory, every file operation deadlocks with no
self-explanatory error (it was an accidental empty directory on 2026-08-13).

Always bump the plugin version for both claude and codex manifests upon any new major feature or functionality or code change

Update README whenever the plugin version changes

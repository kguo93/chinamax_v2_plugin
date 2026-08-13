# IMPLEMENTATION.md — chinamaxM execution order & protocol

Status: **all 11 plans reviewed and PASS** (two independent `/codex-plan-review all`
rounds each — Codex + GLM 5.2 + Kimi K3 — adjudicated and applied in place), then
**reconciled end-to-end on 2026-08-13** (operator grilling): every reviewer-recorded
upstream-doc correction was applied to the ADRs/PRDs/issues/CONTEXT.md directly, and the
plans were updated in place. This file
is the plan-of-record for the implementation phase. **It is not itself an instruction to
start implementing** — implementation begins only on the operator's explicit go-ahead.

Plans live at `docs/plan/*.md` (mirrored to `~/.claude/plans/`). Source PRDs/issues live
under `.scratch/chinamaxm-{proxy,hosts,ops}/` (untracked). Every plan opens with a
read-first `@`-context block; the former per-plan `## Upstream-doc corrections` sections
are gone — their contents are applied.

---

## 0. Preflight decisions — ALL RESOLVED 2026-08-13 (operator grilling)

Implementers apply NO upstream-doc corrections — everything below is already in the
ADRs/PRDs/issues/CONTEXT.md and the plans. Recorded for the record:

1. **Naming**: kebab-case plugin identity — plugin `chinamaxm`, marketplace
   `chinamaxm-plugin`, install id `chinamaxm@chinamaxm-plugin`; the Python package
   stays `chinamaxM` (ADR 0012 as amended).
2. **Canonical root chain**: `constructor/test override → CHINAMAXM_*_HOME →
   $CLAUDE_CONFIG_DIR / $CODEX_HOME → default`, shared by the Proxy and every host
   surface (ADR 0006 as amended); every slice adopts it.
3. **Model-string pivot (the largest change)**: `models[]` and `match` globs are GONE
   from the Registry (ADR 0003); the Anthropic ingress routes by the model string's
   Profile prefix `<profile>/<model>` — strip and forward verbatim, prefixless ⇒
   Default branch, slashed-unknown ⇒ local 404 (ADR 0001); generation emits ONE
   artifact per Profile whose model line is dispatch-mutable state (ADR 0004); `/task`
   NEVER validates a model string — dispatch rewrites the artifact's model line and
   spawns, and provider errors relay verbatim to main (ADR 0005/0007). No litellm at
   dispatch (its catalog demonstrably lags providers; litellm stays Seam-translation
   only per ADR 0002).
4. **Reserved names**: pinned in ADR 0004, enforced at Registry load on both hosts.
5. **Steer verb**: `send_input` stands (ADR 0007 as amended, verified on the installed
   0.147.0); `send_message` recorded as historical.
6. **Supervision**: WinSW runs as the installing user; Setup runs `loginctl
   enable-linger` on Linux (ADR 0009/0005 as amended); `client_max_size` = 256 MiB,
   owned by proxy-01.

Still-live multi-slice conventions:
- **pyproject ownership**: proxy-01 creates `pyproject.toml`; ops-02 extends it.
  Whichever runs second must **extend, never rewrite**; floor `setuptools>=77`.
- **hosts-02 linchpin**: the mid-session model-line-rewrite-then-spawn live check runs
  FIRST in hosts-02 — if the host caches agent definitions, STOP and surface it.

---

## 1. Execution order (strictly sequential — never two plans at once)

Dependency-driven topological order (blockers reflect the reviewer-corrected graph, not
the original issues' understated "Blocked by" lines):

| # | Plan | Depends on (must already be implemented) |
|---|------|------------------------------------------|
| 1 | `chinamaxm-proxy-01-registry-passthrough-skeleton` | — (foundation: `src/chinamaxM` package, `pyproject.toml`, Registry loader, Default-branch passthrough) |
| 2 | `chinamaxm-proxy-02-relay-branch` | proxy-01 |
| 3 | `chinamaxm-proxy-03-responses-seam` | proxy-01 (gated chunk-release fixture), proxy-02 (egress) |
| 4 | `chinamaxm-proxy-04-observability-count-tokens` | proxy-01, proxy-02 (relay egress + `Accept-Encoding: identity` pin), proxy-03 (Seam strip record feeds `stripped_tools`) |
| 5 | `chinamaxm-hosts-01-generators` | proxy-01 (package + Registry) |
| 6 | `chinamaxm-hosts-02-claude-task-contract` | hosts-01 (incl. `set_model`), proxy-01 (Registry loader seam); proxy-02 only for the linchpin live capture through the real relay |
| 7 | `chinamaxm-hosts-03-codex-task-dispatch` | hosts-01, hosts-02 (shared `worker_contract` + symmetry oracle) |
| 8 | `chinamaxm-ops-01-supervision-units` | proxy-01 (package/module + pyproject) |
| 9 | `chinamaxm-hosts-04-diagnosis-surfaces` | ops-01 (status primitives), hosts-01 (drift), hosts-02 (`hooks.json` append), proxy-01/02 (registry/keyfiles), proxy-03 (shared `scan_chat_completions_refs` in `chinamaxM.doctor`) |
| 10 | `chinamaxm-ops-02-manifests-packaging` | proxy-01 (pyproject); **Preflight #1 (naming) resolved** |
| 11 | `chinamaxm-hosts-05-setup` | hosts-01, hosts-04, ops-01, proxy-02, proxy-03 (composes doctor + generators + key scaffold + service + probes) — **last** |

---

## 2. Execution protocol (per plan, in the order above)

For **each** plan, in sequence — **never implement multiple plans concurrently**:

1. **Implement** with a **separate, fresh, zero-context, named Opus subagent at xhigh
   effort** (one dedicated implementer per plan; do not reuse an implementer across
   plans). Hand it only its plan's path; the plan's read-first block carries all context.
   The implementer reads every `@`-referenced file, then implements the slice and its
   tests (there are NO upstream-doc corrections left to apply — the docs are current as
   of 2026-08-13). The
   implementer may use read-only **Explore** subagents but must **not** spawn other
   subagents. **It does NOT self-review and does NOT commit yet.**

2. **Verify** with a **separate Opus verifier subagent** that runs the **`/review`**
   command **once**, ex-post, over the diff for that plan. This verifier is **explicitly
   permitted to launch the two subagents that the `/review` skill requires** (this is the
   sole exception to the "only main spawns subagents" rule). The verifier **messages main
   with a series of corrections**.

3. **Correct & commit.** **Main** passes the verifier's corrections down to that plan's
   **original implementer subagent** (same one — it retains the slice's context). The
   implementer applies the corrections, then **commits** — **exactly one commit per
   plan**. Commit message: what the slice delivered + how it was verified.

4. **Close.** Run **`/close-issues`** on that plan / its issue / its PRD.

5. **Advance** to the next plan in the queue. Repeat 1–4.

6. **Stop only when all 11 plans have been implemented, verified, corrected, committed,
   and closed.**

### Hard rules
- **NEVER EVER COMMIT ANY SECRETS** (no keys, no `model-keys.env` values, no tokens).
  ops-02 ships a tracked-file secret sweep; it must be green before its commit.
- One commit per plan — no batching, no multi-plan commits.
- Strictly sequential: at most one implementer active at a time.
- Verifier runs `/review` exactly once per plan; corrections flow verifier → main →
  implementer; only the implementer commits (after corrections).
- Each plan's verification section lists runnable commands **main** executes (conda env
  `chinamaxM`, pytest, live hook/systemd checks) — run them; do not delegate verification
  commands to the implementer where the plan says main runs them.

---

## 3. Notes
- Live-verification slices (hosts-02/03/04 hooks, ops-01 systemd, hosts-05 `--plan-only`)
  must be exercised for real per their plans, not stubbed.
- The `.scratch/` tree is untracked; ops-02 owns `.gitignore` — until it lands, do not
  `git add` `.scratch/` or `/tmp` review artifacts.
- litellm is not installed anywhere yet; proxy-03's Seam shim rests on litellm 1.96.2
  transform internals — its plan orders the implementer to **confirm-then-STOP** on any
  symbol mismatch and record it as a dated note at the end of that plan rather than
  drift the pin.

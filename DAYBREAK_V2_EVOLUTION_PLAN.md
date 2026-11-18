# Daybreak V2 — Evolution Plan

> **Status:** PLAN FOR OWNER REVIEW. This PR changes no runtime code. It contains no deploys and no merges.
> **Prepared:** 2026-11-18, on branch `plan/v2-evolution`, by the committee roles in the brief: quant research, macro PM, AI-infrastructure equity analysis, options strategy, AI systems architecture, principal engineering, and risk. Claude Code wrote it.
> **Supersedes:** `BUILD_PLAN.md`, which is retained as the record of the completed V1 build (M1–M8).
> Research only. Paper trading only. Not financial advice.

Every claim about the current system cites a repository path (`file:line`) or runtime evidence. Runtime evidence means a Cloud Run execution, a Cloud Logging line, a GCS object, or a command run during this audit. The audit commands are listed in [Appendix A](#appendix-a--audit-evidence-log). LLM spend during verification was **$0.00**: no command in this audit made an LLM call.

---

## 1. Executive decision

**Decision: incremental evolution of the existing codebase. No rewrite and no new repository.**

Justification, from the evidence:

1. **The integrity spine already exists and is the hard part.** That spine has four pieces:
   - the snapshot and as-of discipline (`src/tradingagent/snapshot.py:152-480`);
   - an append-only, provenance-stamped ledger mirrored to GCS (`src/tradingagent/evaluation/ledger.py:1-58`, `src/tradingagent/storage.py:195-236`);
   - a provider-agnostic gateway with schema enforcement and re-prompting (`src/tradingagent/llm.py:165-273`);
   - deterministic trade math (`src/tradingagent/pipeline/trade_plan.py`).

   WS1 needs exactly these four things. Rebuilding them would cost more than the whole of WS1.
2. **The defects are local.** Each of the 6 Phase-0 defects (4 known, 2 found in this audit) has a root cause confined to one or two files (§6, Phase 0). None of them points at an architectural fault.
3. **The pipeline is file-coupled, not code-coupled.** Stages communicate through `reports/<date>/*.json`:
   - `pipeline/context.py:25` writes `discovery-context.json`;
   - `options-context.json` and `presentation-context.json` work the same way.

   So a second window and a separable intelligence module can both attach at the file and ledger boundary without touching the debate pipeline (§3.2).
4. **The suite is healthy.** 736 tests pass in 10.5 s on `main@6b84e79` (audit run, 2026-11-18).

**Dissent recorded (risk officer):** the binding scope inversion makes an unproven trading layer the "actionability layer" of a new module. The outcome evidence for that layer is effectively zero:
- 10 resolved ticker-days;
- 2 of 3 published entries triggered;
- both triggered entries stopped out (`evaluation/2026-47.md`, regenerated in this audit, §5).

The risk officer is not dissenting from evolution. They are dissenting from any wording, in the product or the brief, that implies the trading layer's verdicts have been validated. The plan carries that constraint into §7 and into the brief's language rules (WS2).

---

## 2. Verified capability inventory vs documented

### 2.1 Runtime state (verified 2026-11-18; corrects the brief in two places)

| Fact | Brief / memory said | Verified | Evidence |
|---|---|---|---|
| Scheduler | paused 2026-08-17 → 2026-11-17 | **ENABLED.** Resumed `2026-11-17T07:55:28Z`. Next fire `2026-11-18T13:00Z`. | `gcloud scheduler jobs describe ta-daily-trigger` |
| Total executions | 4 | **5**, all succeeded. The 5th is the first *scheduled* run since August: `ta-daily-99jdc`, 2026-11-17 13:00→13:23 UTC, image `sha256:89a98e77…`. | `gcloud run jobs executions list/describe` |
| Run cost | — | $1.92 est., 465,407 tokens, 79 calls, 5 deep tickers | Cloud Logging, `ta-daily-99jdc` footer |
| Run duration | — | **1,312 s against an 1,800 s task timeout** (73%). Deep stage 1,112 s: 5 tickers, serial, 185–258 s each. | same log, `pipeline/deep.py:347` (serial loop) |
| GIT_COMMIT stamping | added | `GIT_COMMIT=6b84e79a9d7f` on job env; ledger rows from 11-17 carry it | `gcloud run jobs describe`, GCS `runs.jsonl` |
| Models | env-driven | fast=`vertex_ai/claude-haiku-4-5`, smart=`vertex_ai/claude-sonnet-5`, deep=`vertex_ai/claude-opus-5`, `DEEP_TICKER_CAP=5`, `DEBATE_ROUNDS=1` | job env |
| Ledger (GCS) | 20 decision rows / 10 resolved | 9 run rows, 350 candidate rows, 60 decision rows (40 after last-write-wins), 20 outcome rows. Evaluate: 20 observations, 10 resolved. | GCS copy + `--stage evaluate` in an isolated worktree |
| M7 backfill | never run | confirmed: `Backfilled from the pre-ledger journal: 0` | `evaluation/2026-47.md` §1 |
| PR #12 | pending | OPEN, `MERGEABLE/CLEAN`, README-only | `gh pr view 12` |
| stash@{0} | unreviewed | 4 files, +82/−7. `avoid_reason` stops duplicating the rating badge; 4 tests added. Small and coherent. | `git stash show -p stash@{0}` |
| Tests | 736 | 736 passed | `pytest -o addopts="" -q` |
| CI | — | **none.** No `.github/`. The binding "gated auto-merge" workflow needs CI first. | `ls -a` |

### 2.2 Documented capabilities: verified, partial, or contradicted

| Capability (where documented) | Status | Evidence |
|---|---|---|
| Discovery: breadth, sectors, momentum screener, shortlist (README M1) | **Verified.** Runs headless, 110 s zero-LLM locally, ~3 min in cloud. | `--skip-llm` audit run: 498/503 screened, 107 candidates |
| Deep: 12-call TradingAgents debate per ticker (README "The deep stage") | **Verified**, with defect #1: the restatement step returns empty completions | Cloud log: `Restatement CHD/ELV/BAC: Empty completion from tier=smart model=vertex_ai/claude-sonnet-5` |
| One snapshot per run, look-ahead guard (README "One snapshot per run") | **Verified** | `snapshot.py:325-353`; log `Research snapshot snap-2026-11-17-primary-130139Z — market as of 2026-11-16` |
| Signal bundle, 4 sources, shadowed (README M3/M6) | **Partial.** Market news tone is skipped on *every* pre-market run (new defect #5). | `signals/news.py:191-196`; log `cannot be read as of 2026-11-16 — market tone skipped for this backfill` on a live run |
| Options overlay off the Alpaca chain with computed greeks (`data/option_chain.py:16-18`: "indicative … greeks=None") | **Contradicted.** The `indicative` feed now returns greeks and IV (TSM: 2,330 contracts, delta/gamma/theta/vega/rho, IV 0.516). `opra` returns `{"message":"OPRA agreement is not signed"}`. | live read-only probe, 2026-11-18 |
| GCS persistence of reports, journal, ledger (README "Persistence") | **Partial.** Journal and ledger round-trip. Reports are uploaded but **never restored**: `__main__.py:107-116` restores only journal and ledger. `discovery-context.json` is never uploaded. | GCS listing of `reports/2026-11-17/`; `presentation/sheet.py:179-196` reads the local disk |
| "What changed since the last session" diff (M8) | **Inert in Cloud Run** (new defect #6). `previous_context()` walks local `reports/`, which is empty in a fresh container. | `presentation/sheet.py:179-196`, `__main__.py:107-116` |
| Weekly evaluation report (README M7) | **Verified**, with defect #3 | `evaluation/report.py:85` |
| Email with decision sheet, charts, and PDFs (M8) | **Verified.** Emailed 6 attachments and 3 charts. Font gap: `⭑` (U+2B51) renders as `.notdef`. | log 13:23:08–13:23:24 |
| Universe = S&P 500 | **Stale.** Snapshot `as_of 2026-08-14`. 9 names are gone from the live index (AVB, BLDR, CTVA, EQR, KVUE, PSKY, TAP, TTD, WBD). 9 new members are missing (BE, ILMN, OKTA, P, RDDT, SKYD, TWLO, VMRK, VYLR). | `data/sp500.json`; live Wikipedia diff in audit |

### 2.3 Defect register (Phase 0 inputs)

| # | Defect | Root cause (located) | Status |
|---|---|---|---|
| 1 | Sonnet-5 empty completions in the restatement step (CHD, ELV, BAC on 11-17; TSCO and one local earlier) | `restate.py:104-108` calls smart tier with `max_tokens = 2500 + 1.5×chars/3` (`restate.py:48-50`). `llm.py:270-273` raises `EmptyCompletion` but **does not log `finish_reason` or reasoning-token usage**, so "thinking exhausted the ceiling" (the documented failure at `llm.py:336-345`) cannot be told apart from a provider-side empty reply. The re-prompt reuses the same ceiling. | Diagnose with instrumentation first (Phase 0.2) |
| 2 | "yfinance OHLCV DEGRADED in every run" | **Not a feed failure.** `market.py:127-136` flags DEGRADED if *any* symbol of 503 drops. The drops are (a) delisted or merged names still in the stale snapshot (AVB, EQR, KVUE, PSKY, WBD return 0–2 bars: audit probe) and (b) recent spin-offs below the 60-bar floor (FDXF, HONA in August). yfinance returned full history for every live name probed, TSM and ASML included. | Fix the universe and the threshold. Move primary bars to Alpaca SIP, which is free and licensed: verified 261 daily bars for TSM, ASML, NVDA, AVGO, LRCX, GOOG, BRK.B with SIP volume. |
| 3 | Evaluation conflates decision rows with ticker-days ("fully matured 20" > "resolved 10") | `evaluation/report.py:85` counts outcome **rows** (one per decision, not per ticker-day) whose `horizons` dict has ≥5 **keys**. It tests key presence, not resolved values. | Fix and add an invariant test |
| 4 | M7 backfill never run; stash@{0} unreviewed | operational | Phase 0.7, 0.8 |
| 5 (new) | Market RSS news tone is skipped on every live pre-market run | `signals/news.py:191-192`: `if as_of < utcnow().date()`, where `as_of` is the snapshot's *market* date (the prior session). Pre-market, that is always yesterday, so every live run is treated as a backfill. The source stays permanently ungradeable. | Phase 0.5 |
| 6 (new) | Prior-session state unavailable in Cloud Run (the verdict diff is always empty; standalone `--stage deep/options/report` cannot find context) | `__main__.py:107-116` restores only the journal and ledger. `discovery-context.json` / `options-context.json` are not mirrored. | Phase 0.6. **Prerequisite for WS1 change detection and WS2 deltas.** |

---

## 3. Component map

Legend: **KEEP**: unchanged. **EXTEND**: additive change in place. **REFACTOR**: internal change, same contract. **NEW**: new module.

### 3.1 Map

| Component | Files | V2 disposition | Why |
|---|---|---|---|
| CLI / dispatch | `__main__.py` | **EXTEND** | Add `--stage postclose`, `--stage intel`, `--stage weekly`, and `--window premarket\|postclose`. Restore recent report contexts from GCS. |
| Settings | `config.py` (`report_dir()` at :197-198) | **EXTEND** | `report_dir()` gains a window segment for non-premarket windows (§3.2). Add watchlist, Telegram, and intel settings. |
| LLM gateway | `llm.py` | **EXTEND** (Phase 0) | Log `finish_reason` and reasoning tokens on empty completions. Add a provider-agnostic `reasoning_budget` per tier. Otherwise KEEP; WS1 uses it as is. |
| Snapshot / as-of | `snapshot.py` | **KEEP** (+ small EXTEND) | WS1 evidence reuses the `effective_at` and `check()/require()` discipline. A post-close snapshot uses `derive()` (`snapshot.py:239`). |
| Ledger | `evaluation/ledger.py`, `storage.py` | **EXTEND** | New streams added to `STREAMS` (`ledger.py:52-58`) are mirrored automatically (`storage.py:197-236` iterates `STREAMS`). `CandidateRecord` gains `scanner`. Schema v2, defaulting to v1 semantics. |
| Provenance | `evaluation/provenance.py` | **KEEP** | `prompt_versions()` (:132) hashes every prompt in `pipeline/prompts/`, so new `intel_*.md` prompts are versioned for free. |
| Universe | `data/universe.py`, `data/sp500.json` | **REFACTOR** | Membership-tagged universe: `sp500 ∪ ndx ∪ watchlist`. Weekly constituent refresh with the diff logged. Nasdaq-100 source = `api.nasdaq.com/api/quote/list-type/nasdaq100` (verified: 101 rows; the Wikipedia table no longer parses). |
| Bars | `data/market.py`, `data/alpaca_client.py` | **REFACTOR** | Primary = Alpaca SIP daily (free, verified). yfinance as fallback and cross-check. Threshold-based DEGRADED. |
| Fundamentals | `data/fundamentals.py` | **EXTEND** | ADR currency normalisation. TSM `financialCurrency=TWD`, ASML `EUR`, both with `currency=USD` and USD `marketCap` (audit probe). Absolute figures (FCF, revenue) are currently labelled `$` (`fundamentals.py:135`). TSM's FCF of 1.04e12 TWD also breaches the `(-1e12, 1e12)` sanity bound (`fundamentals.py:52`). |
| Breadth | `discovery/breadth.py` | **EXTEND** | Compute over `sp500`-tagged members only, so the composite keeps its meaning (`breadth_series` at :124-135 averages whatever it is given). |
| Momentum screener | `discovery/screener.py` | **KEEP** (baseline / control) | Add a dollar-volume floor beside the share floor (`:408`) for ADRs. Add the AI-focus ordering (§4.3). |
| Pullback/dislocation scanner | `discovery/pullback.py` | **NEW** (the only new scanner) | Treatment arm (§4.3). |
| Sectors / shortlist | `discovery/sectors.py`, `discovery/shortlist.py` | **EXTEND** | AI-focus tags from `config/value_chain.yaml`. Regime-shift gate for non-focus sectors. |
| Calendar / release schedule | `discovery/calendar.py`, `release_schedule.py` | **KEEP** | Reused for the post-close "next window's events". |
| Signals | `signals/*` | **KEEP** (+ fix #5) | No new signal sources (binding). |
| Deep debate pipeline | `pipeline/*` | **KEEP** (+ A/B hook) | §4.4 recommendation. |
| Restatement | `pipeline/restate.py` | **REFACTOR** (Phase 0) | Defect #1 |
| Options | `options/*`, `data/option_chain.py` | **EXTEND** | OPRA feed when the agreement is signed. Vendor greeks/IV as a cross-check, then as primary. |
| Presentation | `presentation/*` | **EXTEND** | Bilingual sheet. Post-close sheet variant. "AI economy — what changed" section fed from a file, not by import. |
| Delivery | `delivery/email.py`, `delivery/stage.py` | **EXTEND** | Window-aware subject and body. Reuse `send_daily_brief` machinery. |
| Telegram | `delivery/telegram.py` | **NEW** | Batched, deduplicated alerts (§4.2). |
| Evaluation | `evaluation/*` | **EXTEND** | Fix #3. Scanner A/B section, thesis-prediction scoring, delivery SLOs. |
| AI-economy intelligence | `src/tradingagent/intel/` | **NEW** (centerpiece) | §4.1 |
| Deploy | `deploy/setup.sh`, `Dockerfile` | **EXTEND** | Second Cloud Run job and trigger. CJK font. CI workflow. |
| CI | `.github/workflows/ci.yml` | **NEW** (Phase 0) | Required by the binding gated-auto-merge workflow |

### 3.2 Audit question: how tightly is the pipeline coupled to the single daily job?

**Moderately, and only through six identifiable seams.** None of them is in the debate code.

1. **Artifact namespace = run date.** `config.py:197-198` puts every artifact in `reports/<run_date>/`. Two runs on one date overwrite each other. This already happened: the supervised run (07:07 UTC) and the scheduled run (13:00 UTC) on 2026-11-17 share `gs://…/reports/2026-11-17/`. Only the ledger, keyed by `run_id`, kept both.
2. **Container default.** `Dockerfile` `CMD ["--stage", "all"]`.
3. **One job, one trigger.** `deploy/setup.sh` hard-codes `JOB="ta-daily"`, `SCHEDULE="0 8 * * 1-5"`, `TIMEZONE=America/New_York`.
4. **Outcome resolution piggybacks on `--stage all`** (`__main__.py:190`).
5. **Cross-run state lives on local disk** (defect #6).
6. **Delivery is invoked only from `--stage all` or `--stage report`** (`__main__.py:135`, `:198`).

**Smallest change that gives a second scheduled window** (about 1 session; Phase 1):

- `Stage.postclose`, plus a `Settings.window` field (`premarket` default, so nothing about today's run changes).
- `report_dir()` returns `reports/<date>/` for premarket (back-compatible) and `reports/<date>/postclose/` for the new window.
- A new `run_postclose()` that is **deterministic and LLM-free**. It reads today's bars for the watchlist and the open trade levels (from the ledger), writes `postclose-context.json`, calls outcome resolution, and sends through the existing email machinery plus Telegram.
- Deploy a **second Cloud Run job**, `ta-postclose`: same image, `--args=--stage,postclose`, 1 vCPU / 512 MiB, 10-min timeout, `--max-retries=0`, with its own Scheduler trigger.

A second job is preferred over one job with run-with-overrides for three reasons:
- It needs no `run.jobs.runWithOverrides` IAM grant.
- It has its own timeout.
- A failure in one window cannot be mistaken for the other in Cloud Logging.

The journal and ledger GCS merge is a line-union (`storage.py:116-136`). Windows ten hours apart cannot race. Even if they overlapped, union-merge would not lose rows.

**Window timing (stated, not assumed).** Both triggers stay in `America/New_York`, because data availability is anchored to the US market:

| Trigger | Cron (America/New_York) | Winter, EST (SGT = ET+13) | Summer, EDT (SGT = ET+12) |
|---|---|---|---|
| premarket (existing) | `0 8 * * 1-5` | 21:00 SGT; delivered ≈21:25 | 20:00 SGT |
| postclose (new) | `30 18 * * 1-5` | 07:30 SGT next day | 06:30 SGT |

The post-close trigger runs about 2.5 h after the close, which is after most after-hours earnings prints. The alternative is anchoring both triggers to `Asia/Singapore`. In summer that would land the premarket run at 09:00 ET, finishing about 09:22, which is 8 minutes before the open. Rejected.

### 3.3 Audit question: can the universe expand without breaking breadth and screener assumptions?

**Yes, with four specific guards.** Sizing (audit, 2026-11-18):
- 15 Nasdaq-100 names are outside the S&P 500: ALAB, ALNY, ARM, ASML, CCEP, CRWV, FER, MELI, MSTR, NBIS, PDD, RKLB, SHOP, SPCX, TRI.
- TSM is in **neither** index and must come from the watchlist.
- AMD, GOOGL/GOOG, MSFT, UNH, NVDA, AVGO and LRCX are already S&P members.
- Net growth is ~503 → ~520 symbols (+3–4%).
- Data volume: +~17 bar series/day, +~5% `candidates.jsonl` rows. Runtime is negligible (discovery is 110 s for 503 names).
- Cost: $0, because bars are free and the deep cap is unchanged.

Guards:
1. **Breadth semantics.** Compute breadth over `sp500`-tagged members only. Otherwise the "% above 50DMA" composite (`breadth.py:124-135`) silently becomes a different index.
2. **ADR liquidity.** The share-volume floor (`screener.py:408`, 1M shares) is not comparable across ADR ratios. Add a dollar-volume floor (proposed $25M 20-day ADV). Keep the share floor for S&P names, so the control arm does not change.
3. **ADR fundamentals currency.** Normalise absolute figures using `financialCurrency`, or render them as ratios only. Until that is fixed, a TSM deep dive would show TWD figures labelled `$` (§3.1).
4. **Sector metadata.** Watchlist ADRs need explicit GICS sector and industry in `config/watchlist.yaml`. They are absent from `sp500.json`. Yahoo's labels ("Technology") map through `normalize_sector` (`universe.py`), but the plan does not rely on that.

Two further quirks to handle:
- Class-share symbols differ by vendor: Yahoo uses `BRK-B`, Alpaca uses `BRK.B` (verified on the Alpaca SIP probe). The bar adapter must map them.
- GOOGL and GOOG would double-count in breadth and in candidates. Keep both for pricing, but count one per issuer (de-duplicate by CIK via `data/entity.py` `IssuerIndex`).

### 3.4 Audit question: where does WS1 attach without entangling the trading pipeline?

At four seams. An import-boundary test enforces them (`tests/test_boundaries.py`, NEW): `intel/` must not import `pipeline/`, `discovery/`, or `options/`, and those must not import `intel/`.

| Seam | Shared component | How WS1 attaches |
|---|---|---|
| LLM | `llm.py` `Gateway.complete(schema=…)` | `intel/` calls the same gateway with pydantic schemas. Prompts live in `pipeline/prompts/intel_*.md` so that `provenance.prompt_versions()` hashes them. |
| As-of | `snapshot.py` | Each evidence item carries `published_at`, `ingested_at`, `effective_at`. A thesis evaluation at window *T* may only read evidence with `ingested_at ≤ T`, using `check()/require()` semantics. Replays are look-ahead-safe. |
| Ledger | `evaluation/ledger.py` | New streams `intel_documents.jsonl`, `evidence.jsonl`, `theses.jsonl`, `predictions.jsonl`, `alerts.jsonl`, appended to `STREAMS` and GCS-mirrored with no new storage code. |
| Delivery | `presentation/*`, `delivery/*` | `intel` writes `reports/<date>/<window>/intel-digest.json`. Presentation reads it **if present**, the same pattern as `presentation-context.json`. No import edge from presentation to intel internals; only the pydantic `IntelDigest` schema lives in a shared `intel/schema.py`. |

The **actionability link** is a read-only lookup. For each ticker linked to a changed thesis, the digest shows its current trade-layer state from today's `candidates.jsonl` and `decisions.jsonl`: screener rank, either scanner's flag, last verdict, and any open plan or overlay. It does **not** queue tickers for deep analysis. That would be a de facto new scanner, which is excluded by the binding decision; see ADR open question Q2.

### 3.5 Audit question: does the deep-debate stage stay as-is for V2?

**Recommendation: KEEP**, unchanged in behaviour, with two changes that make a later A/B cheap:

- **Evidence is insufficient to cut it.** There are 10 resolved ticker-days. Model-tier usage is logged per seat (`evaluation/2026-47.md` §6), but no tier or debate comparison has run. Cutting on priors would repeat the "unsupported claim" error in reverse.
- **Its cost is affordable within budget.** About $1.9/run, about $40/month at 21 sessions (§5). It is the single largest LLM line and still only ~10% of the low end of budget.
- **Make the A/B cheap later:**
  - (a) Add a `DEEP_PROFILE=full|lite` knob. `lite` = analysts → trader → PM, skipping bull/bear/research-manager and the 3 risk seats: 6 of the 12 smart calls per ticker. It is assigned per ticker by deterministic hash `(ticker, date) mod 2` when `DEEP_AB=on`. It enters `config_hash` and per-decision provenance, the same way `PM_TIER` already does (`__main__.py:89-93`, `config.py` `pm_tier`).
  - (b) **Default OFF** until the scanner A/B has 20 resolved observations per arm. Running two experiments on 3–5 daily slots at once would starve both.
- **Runtime risk** is the more pressing issue: 1,312 s of an 1,800 s timeout with a serial deep loop (`deep.py:347`). Phase 3's deep slot for the pullback arm does not raise the cap of 5. The plan nonetheless adds bounded ticker-level concurrency (2 workers) in Phase 0. Tokens stay the same; only wall-clock changes.

---

## 4. Workstream designs

### 4.1 WS1: AI-Economy Intelligence module (centerpiece)

**Purpose.** Maintain a small set of owner-approved, falsifiable theses about who captures value across the AI stack. Attach dated evidence to them from three ingested sources. Emit output **only when something material changed**.

**Package layout (NEW), `src/tradingagent/intel/`:**

```
intel/
  schema.py          # pydantic: Thesis, Evidence, Prediction, IntelDigest, MaterialChange (shared, import-safe)
  theses.py          # load config/theses/*.yaml (owner-authored), version + hash, write theses.jsonl states
  value_chain.py     # load config/value_chain.yaml: layers → tickers (compute, foundry, equipment, memory,
                     #   networking, hyperscaler capex, power/datacenter, software) — owner-curated
  sources/
    edgar_events.py  # 8-K item codes (1.01, 2.02, 5.02, 7.01, 8.01), 10-Q/10-K, AND 6-K/20-F for ADRs
    transcripts.py   # provider adapter (chosen in Phase 5 evaluation); one interface, swappable
    semianalysis.py  # subscriber-delivered email via IMAP label — no site access (ToS, §5.2)
  extract.py         # document → candidate evidence (LLM, fast tier, schema-enforced, quotes ≤ 50 words)
  adjudicate.py      # evidence × thesis → supports/contradicts/neutral + materiality (smart tier)
  change.py          # deterministic material-change rules (below); NO LLM
  predictions.py     # dated, resolvable predictions per thesis; deterministic resolvers where possible
  digest.py          # build IntelDigest for a window; weekly deep-dive assembly
  stage.py           # run_intel(settings, window) / run_weekly(settings)
```

**Reused, not rebuilt:**

| Reused piece | From | Used for |
|---|---|---|
| Throttle, User-Agent policy, ticker→CIK map | `signals/insiders.py:38-75` (`TICKER_MAP_URL`, `SUBMISSIONS_URL`, `_Throttle`) | `edgar_events.py` |
| Issuer resolution | `data/entity.py:139-218` (`IssuerIndex`, `resolve`) | linking a document to tickers |
| Schema enforcement and re-prompt-once | `llm.py:165-215` | extraction and adjudication |
| Change-diff pattern | `presentation/sheet.py:143` `diff_verdicts` | the shape of change detection |

**Thesis record.** Owner-authored YAML. The LLM may *propose* a thesis only in the weekly deep-dive, as a "candidate", and it enters the ledger only when the owner commits the YAML.

```yaml
id: ai-capex-durability-2027
title: Hyperscaler AI capex keeps growing through CY2027
statement: ...
linked_tickers: {beneficiaries: [NVDA, AVGO, TSM, ASML, LRCX], at_risk: []}
value_chain_layers: [compute, foundry, equipment]
confidence: 0.65            # owner-set prior; system proposes moves, owner can override
invalidation:               # deterministic where possible
  - {type: metric, source: transcript, metric: capex_guide_yoy, issuer: [MSFT, GOOGL, AMZN, META], op: "<", value: 0}
  - {type: price_relative, basket: beneficiaries, vs: SOXX, window_d: 60, op: "<", value: -0.15}
  - {type: narrative, text: "two of four hyperscalers cut forward capex guidance"}   # LLM-checked, human-confirmed
predictions:
  - {id: p1, text: "MSFT FY capex guide raised at next call", resolves_by: <earnings date>, p: 0.6, resolver: transcript_metric}
```

**Thesis-ledger streams** (append-only, last-write-wins by key, mirrored to GCS):

- `theses.jsonl`: thesis state per (thesis_id, window_run_id). Carries confidence, evidence counts (supporting/contradicting), invalidation status, `yaml_hash`.
- `evidence.jsonl`: per evidence item. Carries `doc_id`, source, `published_at`, `ingested_at`, issuer/tickers, claim (≤ 50-word quote + paraphrase), thesis links with stance and materiality 0–3, extracting model tier, and `prompt_version`.
- `intel_documents.jsonl`: **metadata only**: source, URL/accession, title, published date, content hash, and token counts. **No full text is persisted** (ToS, §5.2).
- `predictions.jsonl`: per prediction state. Carries the probability at creation and at each revision, the resolution (true/false/void), the resolution source, and the timestamp.
- `alerts.jsonl`: per alert sent or suppressed, with the dedupe key (shared with WS2).

**Material-change detection** (`change.py`, deterministic; this is the "never daily restatement" guarantee). A thesis appears in a window's output **only if at least one** of these holds since the last window in which it was shown:

1. Its invalidation status changed (any condition tripped or cleared).
2. Confidence moved by ≥ 0.10 (system-proposed moves are capped at ±0.10 per window and require ≥1 materiality-≥2 evidence item).
3. A materiality-3 evidence item arrived. These are defined per source: a guidance change in a transcript; an 8-K item 1.01/2.02/5.02 or a 6-K revenue release for a linked issuer; a SemiAnalysis claim that directly contradicts the thesis statement.
4. A prediction resolved.
5. The linked basket moved beyond ±2σ of its 60-day distribution relative to SOXX/QQQ. This is price context only and never by itself changes confidence.

Unchanged theses are counted in one line ("7 theses unchanged") and never restated. Acceptance tests assert this suppression.

**Outputs:**
- **Daily**, in both windows: an "AI economy — what changed" section (0–N items). Each item gives the thesis, what changed, evidence citations with source and date, linked-ticker trade-layer state, and confidence before → after.
- **Weekly deep-dive**: Saturday 07:30 SGT, built from Friday's post-close run. Covers every thesis's state table, evidence added that week, the prediction scoreboard, value-chain "who captured the value" metrics, and candidate new theses. Value-capture metrics are deterministic: linked-layer revenue growth and gross margin from transcripts and filings where extracted, and layer basket excess returns. Email long form only. One smart- or deep-tier synthesis call, capped at 60k input tokens.

**ADR nuance (equity-analyst finding).** TSM and ASML are foreign private issuers. They file **6-K and 20-F, not 8-K or 10-Q**, so an 8-K-only monitor would miss them. TSM's monthly revenue is released via 6-K and is the highest-frequency hard data point in the AI-infra chain, so it gets a dedicated deterministic resolver. EDGAR fair-access limits are 10 req/s with a declared User-Agent (sec.gov "Accessing EDGAR Data", checked 2026-11-18). The existing code already throttles to 5 req/s (`signals/insiders.py:14`).

### 4.2 WS2: Decision-window delivery

**Both windows produce one bilingual decision sheet.** Structure:

```
中文摘要 (Chinese executive summary, ≤ 8 lines)  ← top of email + Telegram
English detail:
  1 Regime          (breadth composite, VIX, SPY/QQQ/SOXX, sector ETF moves; deltas vs prior window)
  2 AI economy — what changed   (WS1 digest; omitted when empty, with the "N unchanged" line)
  3 Levels & triggers            (published entries/stops/targets; CSP/CC strikes vs spot)
  4 Setups / avoids / options    (premarket only — existing M8 sheet sections)
  5 Next window's events         (macro releases, earnings BMO/AMC, linked-issuer filings due)
  6 Evidence so far              (existing weekly line, corrected counts)
  7 Degraded sources · cost · disclaimer (verbatim from config/report-schema.md)
```

**The 07:30 SGT post-close product (NEW). Minimal deterministic content, zero LLM calls:**

| Block | Content | Data source (existing code) |
|---|---|---|
| Closes | Close and % change for SPY, QQQ, SOXX, VIX, sector ETFs, and the watchlist (TSM, ASML, AMD, GOOGL/GOOG, MSFT, UNH, NVDA, AVGO, LRCX + thesis-linked). Excess vs SPY, and vs SOXX for semis. | `data/market.py` / Alpaca SIP daily bar (the day's bar is available ≥15 min after the close: Alpaca docs, research note §5) |
| Regime deltas | Breadth composite now vs this morning's run; VIX change; sector rank changes; breadth-zone transitions flagged | `discovery/breadth.py`; the morning's `presentation-context.json` restored from GCS (needs defect #6 fixed) |
| Triggered levels | For every open published plan (entry/stop/target) and every proposed CSP/CC strike: did today's high/low cross it? Distance to each in % and ATR. | `decisions.jsonl` plans + `evaluation/outcomes.py:236` `entry_status` logic (reused, as-of today) |
| Today's filings and earnings | 8-K/6-K for linked issuers filed today; earnings released AMC today (Finnhub free `/calendar/earnings`: `data/finnhub_client.py:4`, verified free) | `intel/sources/edgar_events.py` (Phase 4). Until then, Finnhub earnings only. |
| Next window's events | Tomorrow's macro releases (FRED/FOMC schedule) and BMO earnings for universe names | `discovery/release_schedule.py`, `discovery/calendar.py` |
| Outcomes | Horizons resolved today | `evaluation/stage.py` `run_outcomes` (moved here from `--stage all`; premarket keeps a fallback call) |

**Chinese summary:**
- Phase 1: templated and deterministic. Fixed phrase table with interpolated numbers, so it carries no translation risk.
- Phase 2: the premarket summary becomes one fast-tier LLM call with a **numeric-consistency validator**. Every number in the Chinese text must appear in the English source facts; otherwise re-prompt once, then fall back to the template. This reuses the "quoted figure disagrees with computed plan" discipline (`pipeline/evidence.py`, `restate.py`).
- Language rule (risk officer): the summary may say "screened / flagged / rated"; it may never say "推荐买入" or "recommend buying", nor claim accuracy.

**CJK rendering.** Email bodies render on the reader's device fonts. The **PDF** path (WeasyPrint) needs a CJK font in the image: `fonts-noto-cjk`, roughly +60–100 MB image size (UNVERIFIED exact size; measure in Phase 2). Today's image ships only `fonts-dejavu-core` (Dockerfile), which already misses `⭑`.

**Telegram channel** (`delivery/telegram.py`, NEW):
- Bot API over HTTPS with `requests`; no SDK dependency. The token is in Secret Manager as `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` is an env var.
- **Batched to windows only.** One message per window run, sent at the end of `run_postclose` or `--stage all`. No polling loop, no webhook, no always-on component.
- **Content:** the 中文 summary, then up to 10 alerts. An alert is a level trigger, a thesis material change, a prediction resolution, or a DEGRADED source on a decision-relevant item. Each alert ends with "details in email".
- **Deduplication:** key = `sha1(alert_type, subject, level_or_thesis_id, state)`. If the same key was sent in either of the previous 2 windows, it is suppressed and logged with `suppressed=true` in `alerts.jsonl`. It re-alerts only on a state change (e.g., stop *hit* after *approached*).
- **Limits:**
  - 4,096 characters per message: overflow is truncated with a count.
  - `parse_mode=HTML`.
  - Telegram asks for at most 1 msg/s per chat. We send at most 2 (summary + alerts).
  - Bot messaging is free.
  - Source: core.telegram.org/bots/faq and /bots/api (Bot API 10.4, 2026-11-09), checked 2026-11-18.
- **Owner feedback without always-on:** at each window start, one `getUpdates` call reads the owner's replies to prior alerts (👍/👎 or "/useful", "/noise") into `alerts.jsonl`. This gives an alert-precision metric (§7) at zero infrastructure cost.
- **Failure policy:** a Telegram failure never fails the run. It is recorded as DEGRADED "Telegram" in the email.

### 4.3 WS3: Trading-layer upgrades

**Universe expansion** (§3.3): `sp500 ∪ ndx ∪ config/watchlist.yaml`.
- Refresh both index lists weekly, in the Friday post-close run. Write the diff as a ledger `runs.jsonl` note and commit nothing at runtime.
- **Focus rule.** The shortlist ordering puts focus-tagged names first: Information Technology, Communication Services, plus value-chain-tagged AI-infra names in Industrials, Utilities and Materials (e.g., power and cooling suppliers named in `value_chain.yaml`). This replaces the `preferred_sector` sort (`screener.py:427`) for the shortlist only. The full candidate pool is still logged, so the control arm remains gradeable.
- **Regime-shift gate.** A non-focus sector may enter the shortlist on a given day only if either:
  - its sector ETF's 20-day relative-strength rank moved ≥ 4 places in 5 sessions, or
  - the breadth zone changed.

  The gate is deterministic and logged.

**The one new scanner, quality-pullback/dislocation** (`discovery/pullback.py`, NEW). It ports RSI-oversold timing from `reference/claude-trading-skills/skills/dividend-growth-pullback-screener` (attribution in PORTING_NOTES). The dividend criteria are dropped because AI-infra names mostly don't pay dividends, and the FMP/Finviz dependence is replaced with our own data.

| Gate | Rule |
|---|---|
| Uptrend | close > 200DMA, and 200DMA slope > 0 over 21 sessions |
| Quality | currency-neutral ratios only (ADR-safe): operating margin > 0, ROE > 0, revenue growth > 0 (ttm), from `data/fundamentals.py`. Missing → ineligible, not passed. |
| Pullback | 8–25% below the 52-week high; RSI(14) ≤ 40 **or** close within 1 ATR of the 50DMA; 5-day down-volume below the 20-day average (orderly, not capitulation) |
| Dislocation variant | 5-day excess return vs sector ETF ≤ −2σ (60-day distribution) with **no** earnings gap in the window. If an earnings gap exists, the candidate is flagged `post-earnings` and kept in a separate bucket. |
| Liquidity | the same dollar-volume floor as momentum, for ADRs |

**A/B wiring into the M7 ledger:**
- `CandidateRecord` gains `scanner: "momentum" | "pullback"` (schema v2; v1 rows read as `momentum`).
- Both scanners log their **full pre-selection pools** every run. The **primary comparison** is pool-level forward excess returns (1/5/20d vs SPY and vs sector ETF) of each scanner's top-k (k=5, 10). It costs no LLM, and n accrues at ~10 names/day per arm.
- **Deep-slot allocation:** 1 of the 5 deep slots is reserved for the top pullback candidate (`PULLBACK_DEEP_SLOTS=1`). The deep cap is unchanged. Decisions carry `arm` in provenance. This decision-level comparison is secondary and explicitly underpowered (§7).
- The existing control/treatment table (signal-adjusted vs shipped, `evaluation/report.py` §4) is unchanged. The scanner A/B is a **new, separate section**, so the two experiments are not conflated.

**Options data:**
- **Step 1 (Phase 0, owner action):** sign the OPRA agreement in the Alpaca dashboard. Then re-run the read-only probe (`scripts/smoke_option_chain.py`, extended).
- **Step 2:** it is **unverified** whether OPRA access on Alpaca also requires the paid Algo Trader Plus plan ($99/month). Alpaca's docs say the chain defaults to `opra` only "if the account has a real-time subscription and has signed the OPRA agreement" (docs.alpaca.markets/reference/optionchain, checked 2026-11-18). Today's error names only the agreement.
  - If signing alone enables `opra`: switch, $0.
  - If a subscription is required: a **decision gate**. Take one month of Algo Trader Plus and measure indicative-vs-OPRA mid and IV divergence on every chosen overlay. Keep it only if divergence changes the chosen strike or moves annualised yield by >10% on >20% of overlays.
- **Step 3:** `options/strategies.py` prefers vendor greeks and IV when present and logs the divergence from `options/black_scholes.py`. The computed values remain the fallback and the audit trail. The scope stays 21–45 DTE CSP/CC only (binding). The stale docstring at `data/option_chain.py:16-18` must be corrected, because the indicative feed now returns greeks and IV.

**Outcome evaluation extensions:** a scanner A/B section, a thesis-prediction scoring section, delivery SLOs, and corrected observation counts (§7).

### 4.4 What stays exactly as-is

- Deep debate prompts and seats, `DEBATE_ROUNDS=1`, the deep cap of 5.
- The signal bundle and its shadow status.
- Paper-only assertions: `ALPACA_PAPER=true` asserted at startup (`__main__.py` docstring, `config.py`) and in `deploy/setup.sh`.
- `--max-retries=0`.
- Journal and ledger merge semantics.

---

## 5. Data sources and cost vs budget ($300–500/month all-in)

### 5.1 Sources (prices checked 2026-11-18; UNVERIFIED where marked)

| Source | Use | Price | Access | ToS for personal LLM ingestion (no redistribution) | Evidence |
|---|---|---|---|---|---|
| **SemiAnalysis** newsletter | WS1 primary analysis | **$70/mo or $690/yr** | Email to subscriber. Public feed at `newsletter.semianalysis.com/feed` (teasers). A private paid RSS URL is **UNVERIFIED**. No newsletter API. The institutional "Model/Research API" is contact-sales. | **Grey.** SemiAnalysis T&C (updated 2026-01-13) §7 prohibits "systematic or automated data collection"; §4 bars embedding into electronic media without permission. Substack ToS bars scraping and storing significant portions. LLM use is not addressed. | newsletter.semianalysis.com/subscribe; semianalysis.com/terms-and-conditions; substack.com/tos |
| **Earnings-call transcripts**: candidate A, API Ninjas | WS1 | Developer **$39/mo** (5 yr); Business $99 | REST | Paid tiers permit storage and commercial use | api-ninjas.com/pricing |
| Candidate B: FMP | WS1 (+ could retire yfinance fundamentals) | Transcripts only on **Ultimate: $139/mo monthly, $99/mo annual**. Starter and Premium exclude transcripts. | REST, 3,000/min | Internal use OK; display or redistribution needs a separate licence | site.financialmodelingprep.com/pricing-plans |
| Candidate C: Finnhub | WS1 | Transcript endpoint "Premium required". **Tier and price UNVERIFIED.** | REST (key already held) | UNVERIFIED | finnhub.io/docs/api/earnings-call-transcripts-api |
| Candidate D: Alpha Vantage | WS1 | Free 25 req/day; premium $49.99–249.99 | REST | UNVERIFIED | alphavantage.co/premium |
| SEC EDGAR | WS1 events; Form 4 already used | free | JSON APIs, 10 req/s, User-Agent required | Public data | sec.gov/os/accessing-edgar-data |
| Alpaca market data, Basic | Bars (SIP daily history verified), indicative options with greeks/IV (verified) | **$0** | 200 calls/min | personal | alpaca.markets/data; audit probe |
| Alpaca Algo Trader Plus | OPRA, if required (§4.3) | **$99/mo** (conditional) | 10k calls/min | personal | alpaca.markets/data |
| yfinance | Fundamentals; bar fallback | $0 | unofficial | Yahoo ToS grey for automated use (pre-existing V1 exposure) | — |
| Finnhub free, FRED, Polymarket, RSS | existing signals and calendars | $0 | existing | existing | README "Data sources" |
| Telegram Bot API | WS2 alerts | $0 | HTTPS | — | core.telegram.org/bots/faq |
| The Information | later option, not now | **UNVERIFIED** (site returned 403 to the audit) | — | UNVERIFIED | — |
| Bar-source backup (only if Alpaca SIP fails acceptance) | Tiingo Power $30/mo; EODHD $19.99–29.99; Massive (ex-Polygon) Starter $29 | — | — | personal licences | vendor pricing pages |

**Transcript provider decision procedure** (Phase 5, before committing; max two paid content sources, per the binding decision):
- Run a 2-week trial on a fixed test set: TSM, ASML, NVDA, AMD, AVGO, LRCX, MSFT, GOOGL, UNH, plus the next 3 reporters.
- Score each provider on:
  - (1) ADR coverage: TSM and ASML present;
  - (2) latency after call end: target ≤ 6 h, so the next window has it;
  - (3) speaker attribution and Q&A segmentation;
  - (4) licence for storage and LLM processing;
  - (5) price.
- **Prior:** API Ninjas, on price and licence clarity. **Fallback:** FMP Ultimate, if ADR coverage or latency fails, because FMP also brings reporting-currency fundamentals that could retire the yfinance ADR quirk (§3.3).

**SemiAnalysis ingestion design (risk-minimised).**
- Read the owner's own subscriber emails from a dedicated Gmail label via IMAP. Reuse the existing Gmail app-password pattern (`SMTP_USER` / `SMTP_APP_PASSWORD` in Secret Manager) with a separate `IMAP_APP_PASSWORD` secret.
- Never access semianalysis.com or Substack programmatically.
- Process each email in memory and persist only `intel_documents.jsonl` metadata plus ≤ 50-word quotes per evidence item.
- **Phase 5 is gated on the owner** either (a) obtaining written permission from SemiAnalysis for personal LLM processing, or (b) explicitly accepting the documented grey-area risk in the ADR. The plan does not decide this for the owner (ADR D6).

### 5.2 Monthly cost projection at V2 cadence

Assumptions:
- 21–22 US sessions/month.
- The LLM baseline is the **measured** run estimate of $1.92/run (11-17 footer; Vertex-billed models above). The ledger's price table is LiteLLM's; Vertex parity with Anthropic list prices is UNVERIFIED.
- Anthropic list prices checked 2026-11-18:
  - Sonnet 5 / 5.5: $2 / $10 per MTok in/out.
  - Haiku 4.5: $1 / $5.
  - Haiku 5.5: $0.10 / $0.50 for ≤100k-token prompts.

| Line | Low | High | Basis |
|---|---:|---:|---|
| Premarket run (existing pipeline incl. pullback slot, still 5 deep tickers) | $40 | $50 | $1.92 × 21–22, + re-prompt headroom |
| Post-close run | $0 | $0 | deterministic by design |
| Chinese summary (premarket LLM, Phase 2) | $0.5 | $2 | 1 fast call ≈ 6k tokens/day |
| WS1 extraction: SemiAnalysis (~8–12 posts × ~25k tokens) | $1 | $4 | fast extract + smart adjudicate |
| WS1 extraction: transcripts (~40 linked issuers/quarter; peak months ~30) | $3 | $15 | ~15k tokens each; smart adjudication vs ≤ 20 theses |
| WS1: EDGAR material filings (~30–60/month) | $0.5 | $3 | item codes are deterministic; LLM only on materiality ≥ 2 |
| Weekly deep-dive | $4 | $12 | 1 smart or deep call/week ≤ 60k in |
| **LLM subtotal** | **$49** | **$86** | ×1.25 contingency → **$61–108** |
| SemiAnalysis | $57.5 | $70 | annual vs monthly |
| Transcripts | $39 | $139 | API Ninjas vs FMP Ultimate monthly |
| Alpaca data | $0 | $99 | OPRA conditional (§4.3) |
| GCP (2 Cloud Run jobs/day, GCS, Scheduler, Secret Manager, Artifact Registry) | $2 | $6 | 22 min + ~3 min runtime/day at 1 vCPU; cloudrun.md estimates < $1/mo for one job |
| Telegram | $0 | $0 | |
| **Total** | **≈ $160** | **≈ $422** | |

**Result:** within the $300–500 budget in every case. The base case uses ~40% of the floor, leaving room to:
- (a) take the OPRA subscription if the gate passes;
- (b) add The Information later, as the owner has noted;
- (c) A/B a model upgrade.

**Cost-lever note (not a recommendation to act yet).** `LLM_FAST_MODEL` → Haiku 5.5 would cut fast-tier list price ~90% vs Haiku 4.5. It is an env-only change, per the CLAUDE.md gateway rule. It changes `provenance.models` and therefore partitions the ledger. Make it deliberately at a phase boundary, never mid-experiment.

---

## 6. Phased roadmap

Ordered by decision value. Each phase is independently shippable. Effort is in **Claude Code sessions** (one focused session ≈ one PR). No calendar dates.

**Why this order:**
- **Phase 1** makes every later output reach the owner at the right hour, and is the cheapest phase.
- **Phase 2** completes the binding bilingual format.
- **Phase 3** comes before the thesis core for two reasons: it starts the A/B evidence clock early (evidence accrues only with sessions), and theses link to TSM and ASML, which must be in the universe to be actionable.
- **Phase 4** proves the thesis loop on free EDGAR data **before** paying for content.
- **Phase 5** onboards paid sources once there is a ledger to absorb them. Vendor lead time (trials, the ToS answer) runs in parallel from Phase 1.
- **Phase 6** spends only on evidence: OPRA is conditional.

### Phase 0: Hygiene, defects, integrity (3 sessions; mostly chore PRs)

| Item | Objective | Reused | New/changed | Acceptance criteria |
|---|---|---|---|---|
| 0.1 CI + gated auto-merge | Enable the binding workflow | `pytest.ini`, `Dockerfile` | `.github/workflows/ci.yml`: pytest, `docker build`, gitleaks secret scan, `tests/test_boundaries.py`. Branch protection; auto-merge allowed only for the `chore` label when paths avoid `pipeline/prompts/**`, `deploy/**`, `Dockerfile`, `config/report-schema.md`, `evaluation/ledger.py`, `llm.py` | CI green on a no-op PR; a test PR touching `deploy/` cannot auto-merge |
| 0.2 Defect #1 restatement empties | Root-cause, then fix | `llm.py`, `restate.py` | Log `finish_reason`, completion and reasoning tokens on `EmptyCompletion`. Re-prompt with ceiling ×2 and an explicit "answer only" instruction. Add a provider-agnostic `LLM_<TIER>_REASONING_BUDGET` passed via LiteLLM with `drop_params`. | Replay of the stored 11-17 CHD/ELV/BAC restatement inputs (≤ $0.50) yields non-empty results, **or** logs show the true cause. Then 5 consecutive scheduled runs with 0 `Restatement` DEGRADED. |
| 0.3 Defect #2 bars | Honest DEGRADED; licensed bar source | `market.py`, `universe.py`, `alpaca_client.py`, `validate.py` | Live constituent refresh plus a fallback snapshot refreshed weekly. New listings below 60 bars → "ineligible (new listing)" note, not DEGRADED. DEGRADED only if > 2% of the universe drops **or** any index/sector-ETF/benchmark/shortlisted/watchlist symbol drops. Alpaca SIP daily as primary; yfinance as fallback with a close cross-check (> 0.5% divergence → note). Symbol mapping `-`↔`.` | `--skip-llm` run on the refreshed universe: no OHLCV DEGRADED. Unit tests for threshold, mapping and new-listing logic. Brief footer names the bar source. |
| 0.4 Defect #3 eval counts | Correct sample language | `evaluation/report.py:85`, `grading.py` | Count matured **ticker-days** whose horizon values are all non-null. Show "decision rows / observations (ticker-days) / resolved / fully matured" separately. | Invariant test: `matured ≤ resolved ≤ observations ≤ decision rows`. Re-render of `2026-47` shows matured ≤ 10. |
| 0.5 Defect #5 news tone | Stop starving a shadow source | `signals/news.py:191` | The backfill test compares `run_date` (decision day) vs the wall clock, not `market_as_of`. Headlines are still frozen into the snapshot (`snapshot.py:290`). | A live premarket run carries `news_tone (market RSS)` readings; a `--date` backfill still skips |
| 0.6 Defect #6 cross-run state | Make prior-window state available in Cloud Run | `storage.py`, `report/writer.py:44` `mirror_json`, `__main__.py:107-116` | Mirror `discovery-context.json` and `options-context.json`. At startup, restore the last 5 sessions' `presentation-context.json`, plus any `postclose-context.json` and `intel-digest.json` once they exist. | In a fresh container, `build_sheet()` returns non-empty `changes` when ratings moved; `--stage report` in cloud re-delivers a past day |
| 0.7 M7 backfill | Recover pre-ledger history | `evaluation/backfill.py` | Run `--stage outcomes --backfill` once, supervised. **Writes to the GCS ledger: owner go-ahead at execution time.** | `Backfilled from the pre-ledger journal > 0` in evaluate; backfilled rows tagged `backfilled` |
| 0.8 stash@{0} | Clear the leftover | stash diff (4 files) | Apply on `chore/avoid-reason-badge`, run tests, PR, or drop with a note | Stash list empty; decision recorded in the PR |
| 0.9 PR #12 | Docs current | — | Manual merge (README-only, CLEAN) | merged |
| 0.10 OPRA | Better options data at $0 if possible | `data/option_chain.py`, `scripts/smoke_option_chain.py` | **Owner** signs the OPRA agreement (free, non-professional individual) in Alpaca. Fix the stale docstring (`:16-18`). Probe both feeds. Log vendor-vs-computed IV and delta divergence. | Probe result recorded in PORTING_NOTES. Either `opra` works, or the error names the subscription (→ §4.3 gate). |
| 0.11 Runtime headroom | Keep the job well under its timeout | `pipeline/deep.py:347` | Bounded 2-worker ticker concurrency, with the shared `TokenLedger` made thread-safe | Run ≤ 15 min at 5 tickers; identical token counts ±10% |
| 0.12 Small | — | `Dockerfile`, `presentation/html.py` | Replace `⭑` or ship a font with the glyph | No `.notdef` warnings in logs |

- **Regression risk:** 0.3 (bar source swap) is the highest. Mitigation: a run-pair comparison on the same date between yfinance-primary and Alpaca-primary; closes must match within 0.5% on ≥ 99% of symbols before the switch.
- **Exclusions:** no model changes, no prompt changes, no new features.

### Phase 1: Second decision window + Telegram (WS2-a), 2 sessions, shippable days after approval

- **Objective:** a deterministic 07:30 SGT post-close sheet, and Telegram batched alerts at both windows.
- **Reused:**
  - `presentation/{build,html,sheet}.py`; `delivery/email.py` (`send_daily_brief`, `build_message`);
  - `evaluation/outcomes.py` (`entry_status`, :236); `discovery/release_schedule.py`; `discovery/calendar.py`;
  - `data/market.py`; ledger `decisions.jsonl` plans.
- **New:**
  - `Stage.postclose` and `run_postclose()` (`stages.py` or a new `postclose.py`);
  - `Settings.window` and window-aware `report_dir()`;
  - `config/watchlist.yaml`;
  - `delivery/telegram.py` and the `alerts.jsonl` stream;
  - templated 中文 summary (`presentation/zh.py`, phrase table);
  - `deploy/setup.sh` second job `ta-postclose` and trigger `30 18 * * 1-5` America/New_York;
  - secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- **Acceptance:**
  - (1) `python -m tradingagent --stage postclose` runs headless on a real trading day with **0 LLM calls**, as asserted from the `TokenLedger`.
  - (2) It writes `reports/<date>/postclose/postclose-context.json` and does not touch `reports/<date>/*.json`.
  - (3) Email and Telegram arrive. A second run the same night sends **no duplicate alerts**: the dedupe test and the `alerts.jsonl` `suppressed=true` rows show it.
  - (4) Premarket output is byte-identical apart from the new Telegram send.
  - (5) Disclaimer present.
  - (6) Tests for dedupe keys, the 4,096-character truncation, and window namespacing.
- **Regression risk:** low, because the change is additive. The `report_dir()` change is guarded by a test that premarket paths are unchanged.
- **Exclusions:** no intel content yet (section omitted), no LLM in post-close, no WhatsApp, no intraday triggers.

### Phase 2: Bilingual decision sheet at both windows (WS2-b), 1–2 sessions

- **Objective:** an LLM-written 中文 executive summary for premarket, validated numerically, plus CJK PDFs.
- **Reused:** the gateway (fast tier), numeric-figure checks from `pipeline/evidence.py` and `restate.py`, `presentation/html.py`, `presentation/pdf.py`.
- **New:** prompt `pipeline/prompts/brief_zh_summary.md`; `presentation/zh.py` validator (numbers ⊆ source facts; banned-phrase list per the risk officer); `fonts-noto-cjk` in the Dockerfile.
- **Acceptance:**
  - A fixture test rejects a summary containing a number not in the source facts.
  - Fallback to the template on a second failure, with DEGRADED "zh summary".
  - The PDF renders CJK, checked by text extraction in a test.
  - Image size growth recorded.
  - Cost ≤ $0.10/day, from the footer.
- **Regression risk:** low. **Exclusions:** no Chinese for deep reports; English detail stays authoritative.

### Phase 3: Universe expansion + pullback/dislocation A/B (WS3-a), 3 sessions

- **Objective:** an AI-focused universe with honest breadth, and start the scanner A/B evidence clock.
- **Reused:** `discovery/screener.py` (control), `discovery/breadth.py`, `discovery/shortlist.py`, `data/indicators.py` (RSI/ATR/MA), `data/fundamentals.py`, `data/entity.py`, `evaluation/record.py`, `evaluation/outcomes.py`.
- **New:**
  - membership-tagged `universe.py` (S&P via Wikipedia, NDX via the Nasdaq JSON);
  - `config/value_chain.yaml`;
  - ADR currency normalisation in `fundamentals.py`;
  - dollar-volume floor;
  - issuer de-duplication (GOOGL/GOOG);
  - `discovery/pullback.py`;
  - `CandidateRecord.scanner` (schema v2);
  - `PULLBACK_DEEP_SLOTS`;
  - evaluation section "Scanner A/B";
  - PORTING_NOTES attribution.
- **Acceptance:**
  - (1) Breadth composite on the expanded universe equals the S&P-only composite (test).
  - (2) TSM fundamentals render in USD or as ratios, never as TWD labelled `$` (fixture from the audit probe values).
  - (3) Both scanners' pools are in `candidates.jsonl` with `scanner` set.
  - (4) One deep slot goes to the pullback arm, tagged in decision provenance.
  - (5) The evaluation report shows the A/B table, with INSUFFICIENT until n ≥ 20 per arm per horizon.
  - (6) Runtime still ≤ 15 min.
- **Regression risk:** medium. The universe change alters the control arm's candidate pool. Mitigation: the control arm's eligibility rules (share floor, S&P membership) are unchanged, so momentum candidates from S&P names are identical to pre-change. A test compares a fixed fixture.
- **Exclusions:** no other scanners, no new signals, no Russell 1000.

### Phase 4: Thesis ledger on free sources (WS1-a), 4 sessions

- **Objective:** a working thesis loop (owner theses → EDGAR evidence → material-change detection → output in both windows → prediction tracking) before any paid content.
- **Reused:**
  - `signals/insiders.py` EDGAR plumbing; `data/entity.py`;
  - `llm.py`; `snapshot.py` as-of checks;
  - ledger and storage; `presentation/sheet.py` diff pattern; WS2 delivery.
- **New:** `intel/{schema,theses,value_chain,change,predictions,digest,stage}.py`, `intel/sources/edgar_events.py`, plus:
  - `config/theses/*.yaml`: **the owner authors 5–8 initial theses**; Claude Code drafts candidates for owner edit;
  - prompts `intel_extract.md`, `intel_adjudicate.md`;
  - streams `intel_documents`, `evidence`, `theses`, `predictions`;
  - `tests/test_boundaries.py` (enforced);
  - TSM monthly-revenue 6-K resolver;
  - evaluation section "Theses & predictions".
- **Acceptance:**
  - (1) `--stage intel --window postclose` runs headless.
  - (2) Day-2 output with no new evidence lists **zero** theses, plus the "N unchanged" line (suppression test).
  - (3) A fixture 8-K item 2.02 for a linked issuer produces a materiality-scored evidence row and, if the rules fire, a digest item with citation.
  - (4) A 6-K for TSM is ingested; ADR path tested.
  - (5) Replaying a window with `ingested_at > T` evidence raises the look-ahead guard.
  - (6) Import-boundary test green.
  - (7) No full document text in any ledger stream (test greps for length caps).
- **Regression risk:** low for trading, which stays separate by boundary test. Medium for cost: monitored by the per-run token footer, with a `INTEL_MAX_TOKENS_PER_RUN` budget cap.
- **Exclusions:** no paid sources, no auto-generated theses entering the ledger, no ticker queueing into deep analysis.

### Phase 5: Paid content + weekly deep-dive (WS1-b), 3–4 sessions

- **Gate:** the owner has (a) subscribed to SemiAnalysis and recorded the ToS decision (ADR D6), and (b) the transcript trial has picked a provider (§5.1).
- **Objective:** add the two highest-signal AI-economy sources and the weekly long form.
- **Reused:** the Phase 4 pipeline unchanged; email/PDF machinery.
- **New:** `intel/sources/transcripts.py` (one adapter); `intel/sources/semianalysis.py` (IMAP); `--stage weekly`; prompt `intel_weekly.md`; secrets `TRANSCRIPT_API_KEY` and `IMAP_APP_PASSWORD`; transcript-metric prediction resolvers (capex guide, data-centre revenue, gross margin).
- **Acceptance:**
  - (1) A transcript for TSM or ASML is ingested within one window of availability.
  - (2) A SemiAnalysis email is processed with metadata-only persistence.
  - (3) The weekly deep-dive is delivered Saturday 07:30 SGT with the prediction scoreboard.
  - (4) The monthly content cost line is visible in the weekly footer.
  - (5) Total spend tracked against §5.2.
- **Regression risk:** low. **Exclusions:** The Information; more than two paid content sources; any redistribution.

### Phase 6: Options data upgrade + evaluation completion + deep A/B hook (WS3-b), 2–3 sessions

- **Objective:** better option quotes where the feed allows; a cheaper future debate A/B; complete measurement.
- **Reused:** `options/*`, `black_scholes.py` (fallback), `evaluation/*`.
- **New:** vendor-greeks preference with divergence logging; OPRA switch or the one-month gate; `DEEP_PROFILE` / `DEEP_AB` knobs (default off); evaluation sections for options-data divergence, delivery SLOs and alert precision.
- **Acceptance:**
  - The options section names its feed.
  - The divergence table populates.
  - `DEEP_AB=on` in a test run splits deterministically and stamps provenance.
  - Default-off produces byte-identical decisions.
- **Regression risk:** medium (options pricing source change). Mitigation: computed values are retained side-by-side for one month.
- **Exclusions:** no strategies beyond CSP/CC; no DTE outside 21–45; no live orders.

### Owner actions (no code; can run in parallel from approval)

1. Sign the Alpaca OPRA agreement (Phase 0.10).
2. Approve the M7 backfill execution (0.7).
3. Create a Telegram bot and share the chat id (Phase 1).
4. Subscribe to SemiAnalysis; decide the ToS posture (D6).
5. Start the transcript trials (§5.1).
6. Draft the initial 5–8 theses (Phase 4).
7. Decide Q1–Q3 in the ADR.

---

## 7. Evaluation strategy

**Principles:**
- No win-rate claims without the existing `MIN_SAMPLE = 20` per cell (`evaluation/grading.py:58`). Below it, the report prints counts and INSUFFICIENT, as it does today.
- Every comparison is against a stated baseline.
- Every number is computed from the ledger, never generated.

| Workstream | Question | Metric | Baselines | Minimum before any claim |
|---|---|---|---|---|
| WS3: scanner A/B (primary) | Does the pullback scanner surface better forward setups than momentum? | Top-k (k=5, 10) pool mean **excess** return at 1/5/20d vs SPY and vs sector ETF; hit rate vs SPY; dispersion; MFE/MAE | momentum pool (control); random-k from the eligible universe (bootstrap, deterministic seed); equal-weight watchlist | 20 resolved sessions per arm per horizon. At ~1 session/day that is ~20 sessions for 1d and ~40 calendar sessions for 20d. Report a CI, not just the mean. |
| WS3: scanner A/B (secondary) | Do deep verdicts on pullback names differ? | Rating distribution, plan trigger rate, stop/target-first, per arm | momentum-arm decisions | 20 per arm. Explicitly labelled "underpowered" until then. |
| WS3: options data | Does OPRA change decisions? | % of overlays whose chosen strike or yield changes by >10% between feeds | computed BS greeks on indicative | 20 overlays |
| Trading layer (existing) | Do verdicts add value? | Existing M7 tables, with corrected counts | SPY, sector ETF, shipped vs counterfactual shortlist | unchanged (20) |
| Deep debate (later) | Does full debate beat lite? | Same verdict metrics by `DEEP_PROFILE` arm; cost per decision | lite arm | 20 per arm. **Not started until the scanner A/B reaches its minimum.** |
| WS1: predictions | Are thesis probabilities calibrated? | **Brier score** per thesis and overall; calibration buckets (0.5–0.6, …); resolution rate; void rate | Climatology (base rate of resolved-true across all predictions); the owner's own prior at creation (the system-revised probability must beat the owner's untouched prior to claim value) | 20 resolved predictions before any calibration statement |
| WS1: material changes | Does a "material change" carry information? | Event study: linked-basket excess return vs SOXX/QQQ at 5/20/60d after a change, signed by direction; owner usefulness ratings (Telegram 👍/👎) | No-change days for the same baskets (placebo); random dates | 20 events |
| WS1: discipline | Is restatement suppressed? | % of window outputs containing an unchanged thesis (target 0%); evidence with citations (target 100%); look-ahead violations (target 0) | — | enforced by tests, reported weekly |
| WS2: delivery | Is the product on time and quiet? | Delivery time vs window (premarket by 21:30 SGT winter / 20:30 summer; post-close by 07:45 / 06:45); duplicate-alert rate (target 0); alerts per window; DEGRADED rate | — | reported from the first run |
| Cost | Is spend buying quality? | $/run by stage and tier; $/material change; content $/evidence item used | §5.2 projection | monthly |

**Thesis scoring rules (committed now to avoid post-hoc choice):**
- Predictions must have a resolution date, a resolver type (deterministic metric or human), and a probability at creation.
- Human-resolved predictions record who resolved them and when.
- A prediction voided for ambiguity counts in the void rate. A void rate > 25% marks the thesis set as "poorly specified" in the weekly report.
- Confidence moves are never graded as predictions. Only stated predictions are scored.

---

## 8. What NOT to build (verbatim from the brief)

**Intraday/streaming, dashboards, portfolio/position accounting, WhatsApp, new agents or debate rounds, new signal sources beyond the stated ones, Russell 1000, compliance/restricted-list features (owner manages personally), live trading of any kind.**

Implied by the above and by the binding decisions, so also excluded:
- Webhooks or any always-on Telegram listener (feedback is read by `getUpdates` at window time).
- More than one new scanner.
- More than two paid content sources initially.
- Options strategies other than 21–45 DTE CSP/CC.
- Ticker queueing from theses into deep analysis (open question Q2).
- Scraping any paid site.
- Persisting full text of licensed content.
- Changes to `DEBATE_ROUNDS` defaults.

---

## 9. Architecture Decision Record

| ID | Decision | Status | Rationale | Recorded dissent |
|---|---|---|---|---|
| D1 | Incremental evolution of the V1 codebase | Proposed | §1: integrity spine reusable; defects local; file-level coupling | **Risk officer:** the trading layer has no validated edge (10 resolved ticker-days; 2/2 triggered plans stopped out). Product language must never imply validation. Accepted as a language rule (WS2). |
| D2 | Scope inversion: AI-economy intelligence as core; trading as actionability layer | **Binding** | Owner decision | **Quant researcher:** thesis value is measurable only via predictions and event studies with slow n (quarterly earnings cadence). Expect ≥ 2 earnings seasons before any calibration statement. Recorded; mitigated by mandatory dated predictions. |
| D3 | Two windows, both cron-anchored in America/New_York; separate `ta-postclose` job | Proposed | §3.2: data availability is market-anchored; independent timeout and IAM | **Macro PM:** SGT arrival drifts 1 h with US DST. The owner may prefer fixed SGT times. A one-line change (`JOB_TIMEZONE`), left to the owner. |
| D4 | Post-close product is deterministic and LLM-free | Proposed | Cost, reproducibility, and it is mostly numbers | none |
| D5 | Alpaca SIP daily bars primary; yfinance fallback | Proposed | Licensed, free (verified), SIP volume; removes reliance on an unofficial scraper | **Principal engineer:** Alpaca free tier is 200 calls/min. ~520 symbols in multi-symbol requests is a handful of calls, but a vendor outage now hits both bars and options. The yfinance fallback is kept for this reason. |
| D6 | SemiAnalysis via owner's own email (IMAP), metadata-only persistence | Proposed, **owner gate** | Avoids site access; minimises stored copies | **Risk officer:** the T&C does not permit LLM processing. Recommends written permission before Phase 5. **AI-infra analyst:** SemiAnalysis is the highest-value source for this thesis set; proceed if the owner accepts the risk. Owner decides. |
| D7 | Transcript provider chosen by a scored 2-week trial; prior = API Ninjas, fallback = FMP Ultimate | Proposed | §5.1 | **Equity analyst:** prefers FMP Ultimate outright, because it also fixes ADR fundamentals and consolidates vendors at $99–139. Recorded; the trial decides. |
| D8 | Deep debate KEEP; add `DEEP_PROFILE` A/B hook default-off | **Binding default** (KEEP) + proposed hook | §3.5 | **Staff AI architect:** the debate and risk committee are 6 of the 12 calls per ticker, all on the smart tier, and have never been tested against a cheaper profile. Would start the lite A/B immediately. Overruled for now to protect the scanner A/B's sample. |
| D9 | One new scanner (pullback/dislocation), pool-level A/B primary, 1 reserved deep slot | **Binding** (scanner) + proposed wiring | §4.3 | **Quant researcher:** a reserved slot means the pullback arm's best name is compared against momentum's top 4, which is a selection asymmetry. Mitigated by making the pool-level comparison primary. |
| D10 | Breadth computed on S&P 500 members only after expansion | Proposed | Preserves the composite's meaning and history | none |
| D11 | Import-boundary test between `intel/` and the trading pipeline | Proposed | Keeps WS1 separable, per the binding decision | none |
| D12 | CI + gated auto-merge for `chore` only; milestones manual | **Binding** | Owner workflow | none |
| D13 | OPRA: sign (free); pay $99 only if a one-month divergence test justifies it | Proposed | §4.3 | **Options strategist:** for 21–45 DTE income trades, indicative "modified" quotes can misstate the premium on wide-spread names. Would pay immediately if signing alone doesn't unlock OPRA. Within budget either way; the evidence gate stands. |
| D14 | Scheduler state is ENABLED (resumed 2026-11-17) | Fact, flagged | Brief said paused | Owner to confirm the resume was intended before Phase 0 code lands; daily runs are spending ~$1.92/session now |

**Open questions for the owner:**
- **Q1:** fixed-SGT vs market-anchored windows (D3).
- **Q2:** may a materiality-3 thesis change claim one deep slot? Recommendation: no for V2. It is a de facto scanner and would contaminate the scanner A/B.
- **Q3:** is the Telegram recipient a private chat (recommended) or a channel?

---

## 10. Prioritized implementation backlog

Order = execution order. `C` = chore (auto-merge eligible after 0.1). `M` = milestone (manual merge).

| # | Item | Phase | Type | Files touched |
|---:|---|---|---|---|
| 1 | CI workflow, secret scan, boundary-test scaffold, branch protection doc | 0.1 | C | `.github/workflows/ci.yml` (new), `tests/test_boundaries.py` (new), `README.md` |
| 2 | Merge PR #12 | 0.9 | M | `README.md` |
| 3 | Resolve stash@{0} | 0.8 | C | `presentation/build.py`, `presentation/html.py`, `tests/test_decision_sheet.py`, `tests/test_presentation_context.py` |
| 4 | Eval counting fix + invariant test | 0.4 | C | `evaluation/report.py`, `evaluation/grading.py`, `tests/test_evaluation_stage.py` |
| 5 | News-tone backfill test fix | 0.5 | C | `signals/news.py`, `tests/test_signals.py` |
| 6 | Empty-completion instrumentation + restate retry policy | 0.2 | M | `llm.py`, `pipeline/restate.py`, `config.py`, `tests/test_llm.py`, `tests/test_restate.py` |
| 7 | Cross-run state restore/mirror | 0.6 | M | `storage.py`, `report/writer.py`, `pipeline/context.py`, `options/context.py`, `__main__.py`, `tests/test_storage.py` |
| 8 | Universe refresh + DEGRADED threshold + new-listing rule | 0.3 | M | `data/universe.py`, `data/market.py`, `data/validate.py`, `data/sp500.json`, `tests/test_discovery.py` |
| 9 | Alpaca SIP bar adapter + yfinance fallback + cross-check | 0.3 | M | `data/alpaca_client.py`, `data/market.py`, `snapshot.py` (source label), `tests/` |
| 10 | Deep concurrency (2 workers) + thread-safe TokenLedger | 0.11 | M | `pipeline/deep.py`, `llm.py`, `tests/test_pipeline.py` |
| 11 | OPRA probe + docstring fix + divergence logging | 0.10 | C | `data/option_chain.py`, `scripts/smoke_option_chain.py`, `PORTING_NOTES.md` |
| 12 | Glyph fix | 0.12 | C | `presentation/html.py` or `Dockerfile` |
| 13 | M7 backfill run (operational, supervised) | 0.7 | ops | none (ledger rows in GCS) |
| 14 | `Settings.window`, window-aware `report_dir()` | 1 | M | `config.py`, `__main__.py`, `tests/test_config.py` |
| 15 | `run_postclose()` + postclose context + sheet variant | 1 | M | `stages.py` or `postclose.py` (new), `presentation/build.py`, `presentation/html.py`, `presentation/sheet.py`, `evaluation/stage.py`, `config/watchlist.yaml` (new) |
| 16 | Telegram delivery + `alerts.jsonl` + dedupe | 1 | M | `delivery/telegram.py` (new), `evaluation/ledger.py` (stream), `delivery/stage.py`, `tests/test_delivery_telegram.py` (new) |
| 17 | Templated 中文 summary | 1 | M | `presentation/zh.py` (new), `presentation/html.py` |
| 18 | Second job + trigger + secrets | 1 | M | `deploy/setup.sh`, `deploy/cloudrun.md` |
| 19 | LLM 中文 summary + validator + CJK font | 2 | M | `pipeline/prompts/brief_zh_summary.md` (new), `presentation/zh.py`, `presentation/pdf.py`, `Dockerfile` |
| 20 | Membership-tagged universe (S&P, NDX, watchlist) + breadth scoping + issuer de-dup | 3 | M | `data/universe.py`, `discovery/breadth.py`, `data/entity.py`, `config/watchlist.yaml` |
| 21 | ADR currency normalisation + dollar-volume floor | 3 | M | `data/fundamentals.py`, `discovery/screener.py`, `config.py` |
| 22 | Pullback/dislocation scanner | 3 | M | `discovery/pullback.py` (new), `data/indicators.py`, `stages.py`, `PORTING_NOTES.md`, `tests/test_pullback.py` (new) |
| 23 | Scanner field (schema v2) + deep-slot allocation + A/B report section | 3 | M | `evaluation/ledger.py`, `evaluation/record.py`, `evaluation/report.py`, `evaluation/grading.py`, `discovery/shortlist.py` |
| 24 | AI-focus ordering + regime-shift gate + `value_chain.yaml` | 3 | M | `discovery/shortlist.py`, `discovery/sectors.py`, `config/value_chain.yaml` (new), `config/preferences.md` |
| 25 | `intel/` schema, theses loader, ledger streams, boundary test enforced | 4 | M | `intel/schema.py`, `intel/theses.py`, `intel/value_chain.py`, `evaluation/ledger.py`, `config/theses/*.yaml`, `tests/test_boundaries.py` |
| 26 | EDGAR events (8-K items, 10-Q/10-K, 6-K/20-F) | 4 | M | `intel/sources/edgar_events.py` (new; reuses `signals/insiders.py` plumbing), `tests/test_intel_edgar.py` |
| 27 | Extraction + adjudication prompts and calls | 4 | M | `intel/extract.py`, `intel/adjudicate.py`, `pipeline/prompts/intel_extract.md`, `pipeline/prompts/intel_adjudicate.md` |
| 28 | Material-change rules + digest + presentation section | 4 | M | `intel/change.py`, `intel/digest.py`, `intel/stage.py`, `presentation/build.py`, `presentation/html.py`, `__main__.py` |
| 29 | Predictions + resolvers (TSM 6-K monthly revenue first) + evaluation section | 4 | M | `intel/predictions.py`, `evaluation/report.py` |
| 30 | Transcript adapter (post-trial) | 5 | M | `intel/sources/transcripts.py`, `config.py`, `deploy/setup.sh` |
| 31 | SemiAnalysis IMAP ingestion (gated on D6) | 5 | M | `intel/sources/semianalysis.py`, `config.py`, `deploy/setup.sh` |
| 32 | Weekly deep-dive | 5 | M | `intel/digest.py`, `pipeline/prompts/intel_weekly.md`, `__main__.py`, `delivery/stage.py` |
| 33 | Vendor greeks preference + OPRA switch/gate | 6 | M | `data/option_chain.py`, `options/strategies.py`, `options/stage.py`, `report/options.py` |
| 34 | `DEEP_PROFILE` / `DEEP_AB` hook (default off) | 6 | M | `pipeline/deep.py`, `config.py`, `evaluation/provenance.py`, `tests/test_pipeline.py` |
| 35 | Evaluation: delivery SLOs, alert precision, options divergence | 6 | M | `evaluation/report.py`, `evaluation/grading.py` |

Every milestone PR also updates `README.md` and `PORTING_NOTES.md` per the Definition of Done.

---

## 11. Drafted CLAUDE.md revision

The proposed `CLAUDE.md` is included **in this PR as a direct edit** so the owner reviews it as a diff. `BUILD_PLAN.md` carries a header marking it as the completed V1 record, superseded by this plan. Merging this PR adopts both.

---

## Appendix A: Audit evidence log (all read-only; no LLM spend)

| Command / probe | Result used in |
|---|---|
| `gcloud scheduler jobs describe ta-daily-trigger --location us-central1` | §2.1 (ENABLED, resumed 2026-11-17T07:55:28Z) |
| `gcloud run jobs executions list/describe` (5 executions) | §2.1 |
| `gcloud run jobs describe ta-daily` (env, timeout 1800 s, 1 vCPU / 1 GiB) | §2.1, §3.5 |
| `gcloud logging read … execution_name=ta-daily-99jdc` | defects #1, #2, #5; cost; runtime |
| `gcloud storage ls/cp` of `journal/` and `reports/2026-11-17/` (copied to `/tmp`, nothing written back) | §2.2, defect #6, §3.2 seam 1 |
| `--stage evaluate` in an isolated git worktree with `REPORTS_BUCKET` and SMTP unset | defect #3, §1 dissent |
| `--stage discovery --skip-llm` in the same isolated worktree | defect #2 reproduction (5/503 dropped, 110 s) |
| `pytest -q` | 736 passed |
| yfinance probe: AVB, EQR, KVUE, PSKY, WBD, TSM, ASML | defect #2 root cause |
| yfinance `.info` for TSM, ASML (`financialCurrency` TWD/EUR) | §3.3 guard 3 |
| Alpaca SIP vs IEX daily bars, 7 symbols; options chain `opra` vs `indicative` | D5, §4.3, §2.2 |
| Wikipedia S&P 500 live diff vs `sp500.json`; `api.nasdaq.com` Nasdaq-100 list | §3.3 |
| `gh pr view 12`; `git stash show -p stash@{0}` | §2.1 |
| Vendor pricing and ToS pages (SemiAnalysis, Substack, FMP, Finnhub, API Ninjas, Alpha Vantage, Alpaca, Tiingo, EODHD, Massive, SEC, Telegram, Anthropic, Vertex) | §5 |

_Automated research output for personal study. Not financial advice. Paper trading only. Verify all data before acting._

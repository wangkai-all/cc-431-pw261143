# Paired analysis console

A small, self-contained system for comparing two groups of **pre-paired**
observations on a missing-prone outcome, with the pairing decided *before* any
outcome is read.

Everything is synthetic. No model is trained, and no result here is presented as
a causal effect.

---

## What it does

Three independent services plus a thin console:

| Process | Port | Responsibility |
|---|---|---|
| `app.observations` | 8101 | Participant covariates, outcome measurements, revision history |
| `app.pairing` | 8102 | Deterministic covariate-only matching, locked plan versions |
| `app.stats` | 8103 | Complete-case analysis, labelled sensitivity analyses, resumable runs |
| `app.web.gateway` | 8100 | Serves the console and proxies the three services |

Each backend can be started, stopped and restarted on its own. They never share
process memory or import one another's state; all coupling is HTTP. Durability is
append-only JSON lines on disk, replayed into an in-memory projection at start.

### Start it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run_stack.py           # console at http://127.0.0.1:8100
```

Then press **Load synthetic data** in the console, build a plan version, and run
an analysis. To start the pieces separately (each in its own terminal):

```bash
.venv/bin/python -m app.observations --port 8101 --data var/data
.venv/bin/python -m app.pairing      --port 8102 --data var/data \
    --observations-url http://127.0.0.1:8101
.venv/bin/python -m app.stats        --port 8103 --data var/data \
    --observations-url http://127.0.0.1:8101 \
    --pairing-url      http://127.0.0.1:8102
.venv/bin/python -m app.web.gateway  --port 8100 \
    --observations-url http://127.0.0.1:8101 \
    --pairing-url      http://127.0.0.1:8102 \
    --reports-url      http://127.0.0.1:8103
```

Seed without the browser:

```bash
.venv/bin/python -m app.common.seeder --base-url http://127.0.0.1:8101
```

Tests (they start real HTTP servers on free ports):

```bash
.venv/bin/python -m pytest -q
```

---

## The rules the system enforces

### 1. Pairing is covariate-only, by construction

`app/pairing/engine.py` takes a covariate table and a `MatchingPolicy`. It has no
parameter that could accept an outcome, and it rejects any candidate row that
carries an outcome-looking field. `GET /api/candidates` on the observation
service returns subjects, sites, bands, arms, baseline values and registration
order — deliberately no outcome column at all.

The policy is validated against an allowlist (`site`, `severity_band` for exact
cells; `baseline_rom` for the caliper), so a caller cannot smuggle a decision
rule keyed on the result.

### 2. Matching choices are pre-declared and version-locked

A plan is created from a policy plus a human-stated **reason**, and is then
immutable. Rebuilding with the same policy and the same covariates is refused
(`409`) — a new version must actually change something. Every version records:

* its policy and a policy checksum,
* the observation-feed fingerprint it was built against,
* the full pair list, every exclusion, and a plan checksum.

`v2` names the version it supersedes. There is no "edit the pairs" endpoint.

### 3. Determinism and ties

Within a cell, intervention participants and controls are ordered by
`(baseline value, sha256(seed:obs_id))`. Shuffling the input produces
byte-identical pairs. Only *exact* ties consult the seed, and a pair formed from
a tie says so (`tie_broken`, `tie_candidates`) rather than hiding it.

### 4. Missing is never zero

The observation API rejects `null`, `NaN`, `Infinity`, booleans and strings for
outcome values. Missingness is the *absence* of a reported measurement, or a
measurement explicitly **withdrawn** with a reason. Withdrawal keeps the old
value in revision history, so the audit trail shows what existed before.

### 5. Corrections are revisions, never overwrites

Every re-entry appends a revision and must carry a note. Analyses bind to a
fingerprint of the *current* revision set; a later correction therefore produces
a different input hash rather than silently changing an old report.

### 6. Complete-case and sensitivity are separate objects

`report["analysis"]["complete_case"]` is the only estimate. It uses pairs with
both outcomes reported and states its coverage (`n` of pairs in plan). It is
labelled as making no missing-data assumption.

`report["analysis"]["sensitivity"]` is a list, each entry carrying its explicit
assumption:

| Scenario | Assumption | Reported as |
|---|---|---|
| `bounded_worst_best` | Missing contrasts lie inside declared bounds (or the outcome's plausible range) | a **bound**, not a CI |
| `tipped_to_null` | Every missing contrast equals zero | an extreme case |
| `imputed_mi` | Missing contrasts drawn from a declared normal model, fixed seed | a model-based **estimate** |

The UI renders these with "not pooled with complete-case" labelling, and the
renderer states the assumption next to the number. The compare view reports
changed pairs when two runs differ.

### 7. Resumable, hashed calculations

A run names its plan version and its outcome; the service fetches the current
outcome rows and that exact plan, then computes one **input hash** over both
before any arithmetic. Stages are `fetch_inputs → assemble_pairs → summarise →
rendered`, and each writes a checkpoint tagged with that hash.

If the process dies mid-run:

* `POST /api/analyses/{id}/resume` refetches and rehashes.
* Matching checkpoints are reused (the console shows how many stages).
* If the observations were corrected or the plan replaced in between, resume
  answers `409` and says the input hash changed — it does not analyse different
  bytes under the old run's identity.

The console has a deliberate fault injector (**Inject interruption at
summarise**) so the recovery path can be exercised without killing anything.

### 8. Exclusions are public

Every participant not in a pair is listed with a reason and a numeric basis:

* `caliper_exceeded` — nearest eligible control's distance, and the caliper,
* `control_pool_exhausted` — no unpaired control remained in the cell,
* `not_selected` — a control that survived the pass but was not needed.

Every pair excluded from the complete-case analysis is listed with which side's
outcome was missing and the observation id.

---

## Synthetic data

`app/common/seed.py` generates the cohort from a fixed seed (`20260101`) across
three sites × three severity bands × two arms, with a planted intervention
effect and planted Gaussian noise. It deliberately includes:

* outcomes drawn missing with probability 0.14 (ROM) and 0.08 (pain); with the
  fixed seed this realises **11 of 108 ROM (10.2%)** and **8 of 108 pain (7.4%)**
  absent,
* one value entered then **corrected** upward (two revisions),
* one value **withdrawn** after entry (now missing, history intact),
* cells with a caliper edge case, so exclusions and their basis are visible.

Re-seeding an already-seeded observation service is idempotent.

---

## Statistical notes

* Paired differences are oriented so positive = favourable; the orientation is
  declared per outcome and echoed in the report.
* The complete-case interval is a Student-t interval on the mean paired
  difference (`n − 1` df). The t distribution is implemented in
  `app/stats/statlib.py` with a continued-fraction incomplete beta; the tests
  check it against published table values.
* Multiple imputation uses Rubin's rules with the standard Barnard–Rubin degrees
  of freedom, and a fixed seed recorded in the output. Repeated runs are
  bit-identical.
* A difference is an association inside the matched sample. The report says so in
  its warnings and the console repeats it in **Scope & interpretation**.

---

## Layout

```
app/
  common/      ids, canonical JSON, append-only journal, seed data, HTTP seeder
  observations/ service: registry, outcome revisions, JSON-safe validation
  pairing/      policy, deterministic engine, versioned plan store
  stats/        t distribution, paired summaries, imputation, report renderer, service
  web/          gateway + single-page console (static/)
run_stack.py    start all four processes
tests/          statlib, pairing engine, analysis assembly, live-service integration
```

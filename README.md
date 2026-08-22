# Recovery Console

An agent that decides how to recover each failed payment, and a console that shows the money it brought back.

Built for **Track 3 — AI Revenue Recovery**.

---

## The problem

A meaningful share of Indian online payments fail on the first attempt — bank downtime, UPI collect timeouts, expired mandates, issuer declines. Most merchants respond with a single blanket retry on a fixed timer. That is cheap to build and it leaves money on the table, because it treats an issuer outage, a low balance, and a frozen account as the same event.

They are not the same event. An outage wants a different rail. A low balance wants the customer. A frozen account wants nothing at all — every attempt spent on it is pure cost.

This system classifies each failure into one of five recovery archetypes, picks an action, runs it past a guardrail layer, and reports the rupees recovered against a fixed-retry baseline.

---

## Result

50,000 failed payments over 14 simulated days, ₹10.3 crore at risk. Same payments, same ordering, same outcome seed in both arms — the only variable is the decision.

|                        |      Baseline |         Agent |     Delta |
| ---------------------- | ------------: | ------------: | --------: |
| Recovery rate          |        25.96% |        48.75% | +22.79 pp |
| **Value recovered**    | **₹2.33 Cr**  | **₹4.43 Cr**  | **+89.8%** |
| Payments recovered     |        12,980 |        24,376 |   +11,396 |
| Attempts spent         |        49,737 |        48,227 |    −1,510 |
| Wasted attempts        |        36,757 |        23,851 |   −12,906 |
| Attempt efficiency     |        26.10% |        50.54% | +24.45 pp |
| p95 decision latency   |      0.005 ms |      0.019 ms |           |

More money from **fewer** attempts. The lift is not "retry harder" — it is spending the same attempt budget on the failures that can actually be recovered.

### Where the lift comes from

Each capability removed in turn, everything else held constant:

| Variant                       |   Recovered |   Rate | Wasted |     vs full |
| ----------------------------- | ----------: | -----: | -----: | ----------: |
| full agent                    | ₹4.43 Cr    | 48.8%  | 23,851 |           — |
| − rail switching              | ₹3.30 Cr    | 38.1%  | 29,160 |   −₹1.12 Cr |
| − customer nudges             | ₹3.59 Cr    | 38.7%  | 28,878 |    −₹83.7 L |
| − issuer health               | ₹4.12 Cr    | 46.0%  | 25,241 |    −₹31.0 L |
| − unrecoverable suppression   | ₹4.49 Cr    | 49.0%  | 25,235 | **+₹6.7 L** |

That last row is a real trade-off, not a win, and it is reported as one. Suppressing known-unrecoverable payments *costs* about ₹6.7 lakh in recovered value. It buys 1,384 fewer wasted attempts and, more importantly, no dunning messages sent to customers whose accounts are frozen or flagged for risk. Whether that trade is worth it is a policy decision for the merchant, not something the agent should quietly decide. The flag is exposed so it can be argued about.

---

## Architecture

```
failed payment
      │
      ▼
┌─────────────────────────────────────────────────────┐
│ TRIAGE                                              │
│   tier 1  rules      catalogued error codes  ~94%   │
│   tier 2  model      free-text acquirer text  ~6%   │
│           └─ falls back to keyword match on failure │
└─────────────────────────────────────────────────────┘
      │  archetype + confidence
      ▼
┌─────────────────────────────────────────────────────┐
│ AGENT                                               │
│   check_issuer_health    burst vs own baseline      │
│   get_customer_history   reliability segment        │
│   schedule_retry / draft_nudge                      │
└─────────────────────────────────────────────────────┘
      │  proposed action
      ▼
┌─────────────────────────────────────────────────────┐
│ GUARDRAILS                                          │
│   kill switch → dry run → idempotency → caps        │
│   → approval queue → audit log                      │
└─────────────────────────────────────────────────────┘
      │
      ▼
 executed │ held │ blocked │ suppressed   → all logged
```

### The five archetypes

| Archetype            | Meaning                                             | Action              |
| -------------------- | --------------------------------------------------- | ------------------- |
| `RETRY_NOW`          | Transient, same rail will work                       | retry at +4 min     |
| `RETRY_AFTER_WINDOW` | Rail degraded, wait for it to clear                  | retry at +25/45 min |
| `SWITCH_RAIL`        | This rail keeps declining                            | re-present elsewhere |
| `CUSTOMER_ACTION`    | Nothing works until the customer fixes something     | templated nudge     |
| `UNRECOVERABLE`      | Do not spend another attempt                         | none                |

### Why rules handle most of it

Tier 1 routes every catalogued error code deterministically. It is free, instant, auditable, and a model would be worse on every one of those axes. The model earns its place only on uncatalogued acquirer strings, where there is no deterministic route.

`evaluate.py` prints a per-tier breakdown so the model's contribution is a measured number rather than a claim:

| Tier              | Decided | Share | Hit rate | Mean latency |
| ----------------- | ------: | ----: | -------: | -----------: |
| rules             |  47,042 | 94.1% |    49.9% |     0.016 ms |
| tier 2 (tail)     |   2,958 |  5.9% |    29.9% |     0.014 ms |

The tail is genuinely harder — a 20-point hit-rate gap against the catalogued head. That gap is the room a model has to prove itself in.

Run with `ANTHROPIC_API_KEY` set and `evaluate.py` also prints a head-to-head scoring the model tier against the keyword fallback on identical payments with an identical outcome seed. If the model does not beat the fallback there, it has no business in the pipeline and should be deleted.

### Guardrails

The question this layer answers: if the model is confidently wrong across 10,000 payments at 02:00, what is the blast radius?

Controls, in firing order:

1. **Scoped kill switch** — suppression is not all-or-nothing. Scopes are additive and can target everything, one issuer, one rail, retries only, or automated customer messages only. In a real incident you usually want to stop retries against one broken issuer while the rest of recovery keeps running.
2. **Dry run** — decides and logs exactly as normal but executes nothing. Lets a policy change be validated against live traffic before it touches money.
3. **Idempotency** — SHA-256 of `(payment_id, attempt, rail)`. A replayed decision collapses to one action.
4. **Attempt caps** — 3 per payment, 4 actions per customer per day, enforced independently of what the agent asks for.
5. **Approval queue** — anything above ₹25,000, or below the confidence floor, is held for a human.
6. **Audit log** — every decision, its inputs, its tier, its reasoning and its guardrail verdict, appended immutably and exportable to JSONL.

Nothing blocked is silently dropped. Every suppressed action leaves a record, including what it *would* have done.

**Message drafting is templated, not generated.** A model that invents an amount or a refund promise in a customer-facing payment message is a compliance problem, not a feature. Templates get reviewed once; generated text would need reviewing every time.

---

## Running it

```bash
pip install -r requirements.txt
```

macOS / Linux:

```bash
./run.sh quick      # 5k smoke run, ~2 seconds
./run.sh eval       # full 50k evaluation + ablation + tier tables
./run.sh console    # live console at http://127.0.0.1:8000
```

Windows, or anywhere `run.sh` is inconvenient:

```powershell
python -m recovery.evaluate --n 5000 --days 7 --no-ablate
python -m recovery.evaluate --n 50000 --days 14
python -m uvicorn api.main:app --port 8000
```

The console takes 10–20 seconds to start — it generates the session and warm-starts 1,200 payments so the first paint is not an empty grid.

Set `ANTHROPIC_API_KEY` to activate the tier-2 model classifier. Without it, the long-tail path runs on keyword fallback, and every report states which mode produced it.

Outputs land in `data/`: `metrics.json`, `metrics.txt`, `audit_log.jsonl`.

### The console

* **Action Required strip** — approvals, value held, issuer alerts and pending follow-ups above the fold, with jumps to each queue.
* **Recovery waterfall** — at-risk value decomposed into recovered segments by decision type, with the baseline's recovery marked as a tick. The agent's bar visibly extends past it.
* **Recovery vs failure age** — recovery probability bucketed by how stale the failure was when the action fired. The decay comes out of the outcome simulator's staleness term, not a hand-drawn curve.
* **Live failure feed** — click any row for the full decision chain: error, issuer state, tools called, reasoning, guardrail verdict, outcome.
* **Issuer health** — every `(issuer, method)` pair as a multiple of its own baseline failure rate. Click through for exposure, top error codes and a suggested action. Pairs below the event floor stay grey and are explicitly flagged as insufficient sample: a 9× burst built from two transactions is not evidence of an incident, and the drill-down says so rather than implying one.
* **Approval queue** — approve or reject held actions, with the guardrail's reason attached.
* **Audit log** — searchable by payment id, archetype, status or reasoning.
* **Controls** — scoped kill switch and dry-run toggle. Engage either and watch execution stop while logging continues.

---

## Honest limitations

**The data is synthetic.** No real transaction data was available, so `generator.py` produces it from explicit assumptions. Distributions were chosen to be plausible, not measured. The two assumptions carrying the most weight on the headline number are `DOWNTIME_SHARE` (27% of failures occur inside an issuer outage window) and the per-code `base_retry_success` values in `error_codes.py`. Those are the first two things to validate against real traffic, and the result should be treated as directional until they are.

**Outcomes are modelled, not observed.** `simulator.py` decides whether a retry succeeds using hidden parameters the agent never sees, which keeps the evaluation non-circular — the agent is scored on outcomes it could not observe when deciding. But it is still a model of reality, and its structure encodes beliefs about how payment recovery works.

**Issuer health is inferred, not known.** In production nobody tells you "HDFC UPI is down" — you infer it from your own failure stream, late and noisily. The monitor reflects this: it reads only observed traffic, never the simulator's downtime ground truth, so it detects outages with a real lag. A version with access to a downtime feed would do better.

**Detection uses a fixed window.** A 20-minute window with a 3× burst threshold is a reasonable starting point, not a tuned one. Low-volume `(issuer, method)` pairs need the event floor to avoid firing on noise, which means genuine outages on quiet rails go undetected for longer.

**The age curve confounds two effects.** Recovery probability by failure age is not monotonic, because the bands are dominated by different archetypes — the 0–5 minute band is almost entirely same-rail `RETRY_NOW` on hard failures, while 5–15 minutes is mostly `SWITCH_RAIL`, which succeeds far more often. The honest reading is that the curve mixes staleness with decision type. Separating decay *within* each archetype is the correct fix.

**One attempt per payment is simulated.** The guardrails support up to three, and the audit log carries the attempt count, but the evaluation scores the first action only. Multi-attempt sequencing is the obvious next thing to build.

---

## A bug worth documenting

The first ablation run showed issuer health *losing* ₹2.7 lakh — removing the capability made the system better.

The cause: on detecting an outage, the agent was downgrading `SWITCH_RAIL` decisions into `RETRY_AFTER_WINDOW` waits. But an outage is scoped to one `(issuer, method)` pair. Switching *away* from the broken rail is the correct response; queuing behind it is the worst possible one. The simulator had the same error mirrored — it applied the downtime penalty to rail-switched retries, which should have escaped it.

Two fixes, one in each file. The capability went from −₹2.7 lakh to +₹31 lakh.

Worth stating plainly: the ablation found this, not inspection. A single headline number would have hidden it completely, because the system still looked good overall while one of its four capabilities was actively destroying value.

A second, smaller instance of the same lesson: the docstrings claimed `evaluate.py` scored the triage tiers separately before that metric actually existed. The claim was written before the code. It is implemented now, and the numbers are in the table above — but a claim in a comment is worth nothing until something prints it.

---

## Layout

```
recovery/
  error_codes.py   taxonomy + hidden ground-truth parameters
  generator.py     synthetic traffic with correlated downtime
  triage.py        rules tier + model tier + keyword fallback
  tools.py         issuer health, customer history, retry, nudge
  agent.py         decision policy, with ablation flags
  guardrails.py    scoped kill switch, dry run, idempotency,
                   caps, approvals, audit log
  simulator.py     ground-truth outcome model
  baseline.py      fixed +30m same-rail retry
  evaluate.py      two-arm evaluation, ablation study, tier tables
api/
  session.py       replay session, age curve, issuer drill-down
  main.py          FastAPI endpoints
web/
  index.html       single-file console
data/
  metrics.txt      committed output of the full 50k run
```

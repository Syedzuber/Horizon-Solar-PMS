# Delivery State — Two Sources, and Why

**Written by prompt 0.6, 29 Aug 2026, as the phase 0 close-out.**

Read this before changing anything that reports delivery state. It exists to answer one
question and answer it definitively:

> *I have found two different sources of delivery state in this codebase. Is that
> deliberate, or is it rot?*

**It is deliberate.** It was decided with the numbers in front of the decider, it is
recorded in three places in the source, and it has a named condition under which it should
be revisited. What follows is the whole of the reasoning, so that nobody has to go and ask.

---

## 1. Which surface reads which source

| Surface | Reads | What that answers |
|---|---|---|
| `dashboard_scm` | `DeliveryChallan` / `DCLineItem`, via `_build_delivery_lookup()` (`views.py:346`) and `raw_challans` (`views.py:1305`) | **Did material physically arrive?** Ordered vs received quantity per BOQ category, damage flags, per-challan status |
| `dashboard_pm` | The same `_build_delivery_lookup()` | The same question, per managed project |
| CEO dashboard, via `_get_ceo_dashboard_context()` (`views.py:1873`) | `ProjectPhase` / `Task` — the Delivery-phase task rows, through `_phase_progress_subqueries()` | **Were the Delivery-phase tasks ticked?** |

Note the shape of this carefully, because the older wording in `docs/execution-model.md`
§6 said "the SCM one" and that undersells it: the challan-backed read serves **two**
dashboards, PM and SCM, through one shared helper. The task-derived proxy serves **one**,
the CEO's. Three surfaces, two sources, one deliberate divergence.

`_build_delivery_lookup()` is itself shared for exactly the reason this document exists —
its docstring says so:

> *"Called by both dashboard_pm and dashboard_scm — shared to prevent independent
> implementations drifting apart (the root cause of the Q4 representation-5 drift)."*

So the duplication that survives is **one considered divergence**, not a habit. Where the
same question is asked twice, the code shares an implementation. Where two different
questions are asked, they have two implementations, and this file names them.

---

## 2. What the code already says

The rationale is written at `projects/views.py:1642-1653`, immediately above the position
constants. Quoted rather than paraphrased, because a paraphrase is how an instruction gets
softened into a suggestion:

```
# DELIVERY AND BOQ ARE TASK-DERIVED PROXIES, NOT RECORDS OF THE THING THEY NAME.
#
# `Delivery` reads "were the Delivery-phase tasks ticked", not "did material arrive".
# `BOQ` reads "was the BOQ Preparation task ticked", not "does a BOQ document exist".
# DeliveryChallan and BOQ/BOQItem are the semantically correct sources and are
# deliberately NOT consulted here: production holds 1 challan and 2 BOQ rows across 28
# active projects, so a card built on them is blank on 27 of 28 cards. The phase/task
# data is populated because that is where people actually work.
#
# This is a considered trade of precision for coverage. Do not "improve" it by
# cross-checking against the SCM models — the two will disagree, and the disagreement
# is not a bug in either. See SECONDARY_FINDINGS.md.
```

`SECONDARY_FINDINGS.md` carries the same finding from the other end — from the session
that measured the data rather than the session that wrote the card:

> *"`Delivery` and `BOQ` on the CEO card are task-derived proxies, not records of the
> thing they name. […] A project can show `Delivery: Done` with no `DeliveryChallan` row
> at all, and `BOQ: Done` with no `BOQ` row — on current production data, most of them
> do."*

> *"`DeliveryChallan` remains the semantically correct source for delivery status, and
> `BOQ`/`BOQItem` for BOQ status. They were not used because production holds 1 challan
> and 2 BOQ rows across 28 active projects, so a card built on them renders blank on 27 of
> 28. Once the SCM workspace is genuinely populated, these two fields should move to the
> real models — at which point the task-derived and challan-derived answers will disagree
> on historical projects, and that disagreement is not a bug in either."*

Two independent sessions, the same conclusion, reached from opposite directions. That is
the strongest evidence available that this is a decision rather than an accident.

---

## 3. Why the duplication exists

The two sources answer **different questions**, and only one of them has data.

**The semantically correct source is nearly empty.** At the time of the measurement,
production held **1 delivery challan and 2 BOQ rows across 28 active projects**.
`SECONDARY_FINDINGS.md` adds the detail that 6 challans existed in total — 4 Expected, 1
Partially Received, 1 Received — and that no row has ever held `REJECTED`. A CEO portfolio
card built on `DeliveryChallan` would therefore have rendered **blank on 27 of 28 rows**.

**The proxy source is populated**, because phases and tasks are where people actually work.
Every activated Residential project gets 9 phases and 52 tasks, and the Delivery phase's
five tasks get ticked in the ordinary course of the job whether or not anyone opens the SCM
module.

So the choice was between a precise card that is empty and a coarse card that is
populated. **The coarse card was chosen, knowingly.** A dashboard row that is blank for 96%
of the portfolio is not more honest than a proxy — it is simply unread, and an unread row
teaches its readers to ignore the whole card.

**This is a trade of precision for coverage.** Both halves of that sentence are load-bearing.
The cost is real: `Delivery: Done` on the CEO dashboard genuinely does not mean material
arrived, and anyone reading it as though it does will be wrong. The benefit is also real:
the card is legible on 28 of 28 projects instead of 1 of 28.

---

## 4. The condition under which this should be revisited

**This is the part of the document that matters most, and it is the part a future session
is most likely to skip.**

The coverage argument is the *whole* justification. It is an argument about a fact of the
data, not a claim about which model is correct — the code itself concedes that
`DeliveryChallan` is "the semantically correct source". **The moment the fact changes, the
justification expires.**

Revisit when **real execution data exists** — concretely, when the SCM workspace is
genuinely in use and delivery challans exist for a substantial majority of active
projects rather than for one of them. At that point:

- the coverage argument no longer holds, because a challan-backed card is no longer blank;
- the precision cost stops being a fair price, because it is no longer buying anything;
- the CEO's Delivery and BOQ fields **should move to `DeliveryChallan` and `BOQ`/`BOQItem`**,
  as `SECONDARY_FINDINGS.md` already anticipates.

**Two things to know before making that move.**

1. **The two answers will disagree on historical projects, and neither is wrong.** A project
   whose Delivery tasks were ticked in an era when nobody recorded a challan will read
   `Done` under the proxy and `nothing` under the challan. That is a true statement about
   two different things, not a data-quality bug to be reconciled. Do not write a backfill
   that invents challans to make the old cards agree with the new ones.
2. **`DCLineItem` has no join key to `BOQItem`** — only a `boq_category` CharField and a
   free-text `item_description`, with four category values against `BOQItem`'s five, so
   anything filed under `Other` is unreconcilable by construction. That is **B-18** in
   `docs/execution-model.md` §8, and it is unanswered. A challan-backed card that wants to
   say "received against BOQ" needs B-18 settled first; a card that only wants to say
   "something was received" does not.

**Until that condition is met, do not touch this.** Specifically:

- Do not "fix" the CEO card by cross-checking it against `DeliveryChallan`. The code
  forbids it by name, and the reason is above.
- Do not collapse the two into one helper. They are not two implementations of one
  question; sharing them would silently pick a winner.
- Do not delete the proxy as dead code. It is the only thing populating those cards.
- Do not treat a disagreement between the two dashboards as a bug report. Send the reporter
  here.

**Revisiting is a decision, not a tidy-up.** It changes what a number on the CEO's screen
means, and the person who reads that screen should know it changed. It needs the product
owner, a measurement of the current challan coverage, and its own prompt — not a session
that happened to be passing.

---

## 5. Related records

| Document | What it holds |
|---|---|
| `SECONDARY_FINDINGS.md` | The measurement — challan and BOQ row counts on production, the never-rendered `REJECTED` branch, and the original statement of the trade |
| `docs/execution-model.md` §6 | The one-paragraph summary, in the "still open" list, pointing here |
| `docs/execution-model.md` §8, **B-18** | The missing `DCLineItem` → `BOQItem` join key, which any challan-backed reconciliation depends on |
| `projects/views.py:1642-1663` | The in-code rationale and the Residential-only position constants |
| `projects/views.py:346` | `_build_delivery_lookup()`, the shared challan-backed read |

---

## 6. One more thing the constants assume

The proxy is **Residential-only**, and the code is explicit that this is not incidental.
`RESIDENTIAL_DELIVERY_PHASE_ORDER = 6` and `RESIDENTIAL_BOQ_PHASE_ORDER = 3` are positions
in `build_residential_phases()`. There is no OPEX or CAPEX phase template in this codebase
at all — non-Residential activation tells the PM to add tasks by hand — so "phase 6" of a
tender site would be whatever somebody happened to create sixth. Both subqueries therefore
carry their own `project_type='Residential'` term and both rows hide for other types.

**If prompt 1.3 creates an OPEX template, these constants do not automatically become
valid for it.** They encode positions in one specific list. A new template with a Delivery
phase in a different position makes them silently wrong rather than loudly wrong, which is
the worse failure. Whoever builds that template owns checking this.

# Phase 4 preparation: six Grok agents in advisory-only mode (not activated)

Status 2026-11-09: **contracts, desk orchestration, separate advisory store, gated xAI transport and tests are
implemented. No xAI call has been made.**
- Activation needs operator approval (section 5).
- Kronos remains the only forecaster.
- The shadow runners, frozen strategies and `trading.risk.evaluate` cannot receive agent output.

## 1. Review of the existing agent components (reused, not duplicated)
| Component | Where | Used now |
| --- | --- | --- |
| Approved roles and provider rule | `CLAUDE.md`, `docs/ARCHITECTURE.md` | Scout, News, Chart, Kronos Analyst, Skeptic and Coordinator, all on the official xAI Grok API |
| News contract | `trading/agents/news.py` (`parse_news_assessment`, `NewsAgentPort`, `advisory_guard`) | The News role is validated with it, against a read-only snapshot of the runner's verified calendar |
| `GrokNotActivated`, `OFFICIAL_XAI_BASE_URL`, recorded advisory interfaces | `trading/agents/mtf_advisory.py` | Reused by the transport and gate |
| Phase 1 six-agent desk | `research/six_agent_desk.py`, `docs/six-agent-desk.md` | Legacy BTC paper-eligibility prototype (role verdicts approve/reject). **Not** used for advisory mode, because its role verdicts gate paper eligibility. The new desk gives agents no approval power. |
| Kronos forecast API | `research/forecast_service.py`, `docs/grok-forecast-api.md` | Kronos Analyst input. Forecasts are passed as **recorded** records with ids; the agent cannot forecast. |
| Guard test | `tests/test_trading_gate.py::test_no_ai_provider_in_deterministic_decision_path` | Still passes. `trading/` holds contracts only; all network code is in `research/grok_advisory.py` |

## 2. What was added
- **`trading/agents/desk.py`: strict output contract (schema v1) for Scout, Chart, Kronos Analyst, Skeptic and
  Coordinator.**
  - Recommendations are limited to `no_view`, `caution` and `avoid_new_entries`. The Coordinator may also return
    `insufficient_inputs`.
  - **Rejected outputs:**
    - any controlled field at any depth (entry, stop, target, size, risk, limits, params, schedule,
      execution_mode, approve/veto/override, side/action);
    - a foreign provider, wrong role or wrong subject;
    - evidence ids not in the context;
    - a stale or future timestamp;
    - an out-of-range confidence or validity.
  - **Role-specific rules:**
    - Chart levels must lie inside the supplied price range.
    - The Kronos Analyst may cite only a supplied forecast id, and must give `no_view` without one.
    - The Coordinator must list exactly the validated inputs it received, and must answer `insufficient_inputs` if
      any is missing.
  - **Fallback:** a deterministic Coordinator fallback records "no complete view". It never relaxes anything.
- **`research/grok_advisory.py`:**
  - **Read-only context builder** (SQLite `mode=ro`):
    - the signal;
    - up to 96 bars strictly **before** the signal close;
    - the frozen decisions already taken;
    - recorded Kronos forecasts, if supplied.
  - **Calendar snapshot:** the runner's `calendar.sqlite` is copied through a read-only connection.
  - **Separate store** (`state/grok_advisory/advisory.sqlite`). It records:
    - one row per call, with prompt, response and context SHA-256;
    - raw outputs with their validation result;
    - the Coordinator summary.
  - **Transports:**

    | Transport | Behaviour |
    | --- | --- |
    | `dry-run` | No calls (default) |
    | `replay` | Recorded responses |
    | `xai` | `https://api.x.ai/v1/chat/completions` only; temperature 0; JSON output; no tools |

  - **xai transport safeguards:**
    - the key is read from `XAI_API_KEY` at call time;
    - the key is redacted from errors and never stored;
    - a daily call budget applies.
  - **Read-only CLI:** `review`, `status`.
- **`research/configs/grok_advisory.json`:** `activated: false`, `model: null`, `daily_call_budget: 60`. It lists the
  three runners' stores.
- **Tests:** `tests/test_grok_advisory.py` (27 tests). No network; the xAI transport is exercised through a fake
  opener.

**Live dry run (2026-11-09):** `review --runner xau_orb --transport dry-run` read the running Phase 2D store
read-only. It found 0 live signals, because the forward window starts 2026-11-09. The calendar file hash was
unchanged.

## 3. Data flow (advisory only)
```
shadow.sqlite (read-only) ─┐
calendar snapshot (copy) ──┼─> context (evidence ids) ─> 5 specialists ─> validate ─> Coordinator ─> validate
recorded Kronos forecast ──┘                                     │                                  │
                                                                 └──── advisory.sqlite (separate) <─┘
strategies ─> signals ─> trading.risk.evaluate ─> simulated fills     (no arrow from the advisory store)
```

## 4. Not yet done (Phase 4 plan, section 6)
- No real xAI call has been made, and the model id has not been chosen.
- The xAI endpoint, JSON-mode and model names must be checked against docs.x.ai at activation.
- There is no scheduler: reviews run only on demand.
- There is no Kronos forecast feed for XAUUSD; the Kronos model in this repository is evaluated on BTC.
- There are no headlines source or source-verification adapter for News.
- The optional `advisory_guard` (News `avoid_new_entries` → temporary restriction) is **not** wired into any runner.
  It could only add restrictions. It would change a frozen experiment's behaviour, so it is excluded from all
  running and prepared forward tests.

## 5. Activation procedure (operator approval required)
1. Approve paid xAI usage and a daily budget.
2. Choose the model id. Set `"activated": true` and `"model"` in `research/configs/grok_advisory.json` in a
   reviewed commit.
3. Store `XAI_API_KEY` outside Git, using a user-scoped environment variable or a secrets store. Set
   `GROK_AGENTS_ACTIVATED=1`.
4. Run
   `python -m research.grok_advisory review --runner xau_orb --transport xai --i-have-activation-approval --latest 1`,
   then inspect `advisory.sqlite`.
5. Only then schedule periodic reviews. They stay advisory and are recorded separately.

## 6. Phase 4 implementation plan
| Step | Deliverable | Gate |
| --- | --- | --- |
| 4.1 | This milestone: contracts, desk, store, gated transport, tests | Done (PR) |
| 4.2 | Record-only Kronos forecast feed for the runners' markets. BTC 1h exists (`research/forecast_service.py`); XAUUSD needs a forecast evaluation first. Forecasts are stored with ids and SHA-256. | Kronos evaluation on XAUUSD; no trading use |
| 4.3 | Activation smoke test on 1–3 recorded signals (budget ≤ 20 calls) | Operator approval plus key |
| 4.4 | Scheduled advisory reviews of new shadow signals. These run in a separate process with its own task and state. The heartbeat/health report covers it. | Approval of Task Scheduler change |
| 4.5 | Evaluation: are advisory recommendations informative? | Compare `caution`/`avoid` against the realised R of the frozen shadow trades, after ≥ 100 reviewed signals. Pre-register the comparison before looking. |
| 4.6 | Any use of advice in decisions (for example an opt-in `advisory_guard` that only adds restrictions) | A **new** pre-registered experiment. Never retrofitted into a running forward test |

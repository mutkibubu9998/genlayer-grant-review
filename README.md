# GrantReview — Grant Proposal Scoring by GenLayer Consensus

A standalone GenLayer Intelligent Contract that scores grant proposals:
anyone registers a proposal (title, description, requested amount in USD
cents, scoring criteria URL, evidence URL/text), and anyone can trigger
a review. The leader fetches the criteria page and the evidence and an
LLM scores the proposal 0–100 across four criteria — impact, feasibility,
clarity, budget reasonableness (each 0–25) — returning strict JSON with a
short rationale. The total is the deterministic sum of the four criteria
(computed by the contract, never trusted from the LLM) and the verdict
band is derived from the total: 80–100 `strong`, 60–79 `moderate`, 40–59
`weak`, 0–39 `reject`.

**Deployed on StudioNet (gasless):** `0x62c66E88568aE81b33f5593C32A67aDcE866DD19` — see
[explorer](https://explorer-studio.genlayer.com/address/0x62c66E88568aE81b33f5593C32A67aDcE866DD19)

Deploy transaction: `0x91585c72c0591644059ff0e7c10273a6c23207ac4450686931af3ef96698ce29` (ACCEPTED, 4/5 validators agreed, 1 idle).

## How it works

1. **Anyone registers a grant proposal** (`register_proposal`): title,
   description, requested amount in USD cents, scoring criteria URL, and
   evidence (URL and/or text).
2. **Anyone triggers a review** (`review`). The leader fetches the criteria
   page and the evidence page and an LLM scores the four criteria.
3. **Validators independently re-fetch the same criteria and evidence and
   re-run the same scoring.** The custom comparative validator accepts
   the review only when the validator total is within ±10 points of the
   leader total AND both derive the same verdict band — no single leader
   can set a score alone.
4. **The contract owner can close/withdraw a proposal**
   (`close_proposal`), from OPEN or REVIEWED state.

No money moves in this contract at all: no GEN transfers, no payments, no
payable methods. It is a pure scoring primitive — the same contract serves
any grant program because the proposal content, the criteria, and the
evidence are per-proposal inputs, not hard-coded prompts.

## State machine

```
OPEN ──review (consensus score + verdict)──▶ REVIEWED
  │                                           │
  └────────────close_proposal─────────────────┘──▶ CLOSED
```

## Why this is a reusable primitive, not a thin LLM wrapper

- The proposal, the scoring, and the state transition are all on-chain
  and gated by independent validator re-verification of the criteria and
  evidence — the LLM never decides alone.
- Equivalence is comparative on the decision fields (total within ±10 AND
  same verdict band); free-text rationales may differ.
- The total score is derived deterministically in the contract from the
  four criterion scores, and the verdict band is derived from the total —
  neither is trusted from the LLM output.
- Error classification follows the GenLayer convention
  (`[EXPECTED]` / `[EXTERNAL]` / `[TRANSIENT]` / `[LLM_ERROR]`) so
  validators compare failure paths correctly.
- The contract is generic: one deployment serves any grant program's
  proposal scoring.

## Contract methods

| Method | Type | Description |
|---|---|---|
| `register_proposal(title, description, amount_cents, criteria_url, evidence_url, evidence_text)` | write | Register a grant proposal (anyone), returns `prop_N` |
| `review(proposal_id)` | write | Consensus scoring, returns total score + verdict band |
| `close_proposal(proposal_id)` | write | Owner closes/withdraws a proposal |
| `get_proposal(proposal_id)` | view | Full proposal record |
| `list_proposals()` | view | All proposals |
| `get_proposal_count()` | view | Number of proposals |

## Tests

```bash
# Lint (genvm-lint 0.11.0)
genvm-lint check contracts/grant_review.py

# Direct mode (fast, in-memory; leader only, mocked web/LLM)
pytest tests/direct/ -v

# Integration (GLSim, full leader + validator consensus)
glsim --port 4000 --validators 5
gltest tests/integration/ -v -s --network localnet
```

- Direct: 8 tests — registration + validation, review strong/moderate/
  weak/reject bands, text-only evidence, LLM validation, close
  (owner-only), close after review.
- Integration: 4 tests — full register → review strong flow, review
  reject flow, close owner-only + review of a closed proposal fails,
  review of an unregistered proposal fails.

## Deploy (StudioNet, gasless)

```bash
genlayer network set studionet
genlayer deploy --contract contracts/grant_review.py
```

> Note: `genlayer deploy` (CLI 0.39.2) hangs after printing the deployment
> parameters when run under a PTY on this machine (spinner loop, no tx
> submitted). The same deploy works in seconds via
> [`deploy_direct.cjs`](deploy_direct.cjs), which calls `genlayer-js`
> directly with the `genlayer-builder` keystore (password read from
> `~/workspace/genlayer/.builder-account-password` at runtime, never
> stored in the repo):
>
> ```bash
> node deploy_direct.cjs
> ```

## License

MIT — see [LICENSE](LICENSE).

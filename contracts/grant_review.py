# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""GrantReview - grant proposal scoring by GenLayer consensus.

Anyone registers a grant proposal: title, description, requested amount
(in USD cents, u256), the scoring criteria URL, and evidence (URL and/or
text). Anyone can then call review: the leader fetches the criteria page
and the evidence and an LLM scores the proposal 0-100 across four criteria
(impact, feasibility, clarity, budget reasonableness), each 0-25, returning
strict JSON with per-criterion scores plus a short rationale. The total
score is the deterministic sum of the four criteria (computed by the
contract, never trusted from the LLM), and the verdict band is derived
from the total: 80-100 strong, 60-79 moderate, 40-59 weak, 0-39 reject.

Every validator independently re-fetches the same criteria and evidence
and re-runs the same scoring; the review is accepted ONLY when the
validator total is within +/-10 points of the leader total AND both
derive the same verdict band, so no single leader can set a score alone.

State machine:
    OPEN -> REVIEWED       (consensus score + verdict band stored)
    OPEN -> CLOSED         (contract owner closes/withdraws the proposal)
    REVIEWED -> CLOSED     (contract owner closes after review)

No money moves in this contract at all: no GEN transfers, no payments, no
payable methods. It is a pure scoring primitive - the same contract serves
any grant program because the proposal content, the criteria, and the
evidence are per-proposal inputs, not hard-coded prompts.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *

# ---------------------------------------------------------------------------
# Error classification (validators compare these prefixes, see _handle_leader_error)
# ---------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"  # Business logic (deterministic) - exact match required
ERROR_EXTERNAL = "[EXTERNAL]"  # External API 4xx (deterministic) - exact match required
ERROR_TRANSIENT = "[TRANSIENT]"  # Network/5xx (non-deterministic) - agree if both transient
ERROR_LLM = "[LLM_ERROR]"  # LLM misbehavior - always disagree, force rotation

MAX_TITLE_LEN = 200
MAX_DESCRIPTION_LEN = 2000
MAX_URL_LEN = 500
MAX_EVIDENCE_LEN = 2000
EVIDENCE_CHARS = 6000
SCORE_TOLERANCE = 10
MAX_CRITERION_SCORE = 25
VERDICT_BANDS = ("strong", "moderate", "weak", "reject")


def _verdict_band(total: int) -> str:
    """Deterministic band from a 0-100 total score."""
    if total >= 80:
        return "strong"
    if total >= 60:
        return "moderate"
    if total >= 40:
        return "weak"
    return "reject"


@allow_storage
@dataclass
class Proposal:
    id: str
    proposer: Address
    title: str
    description: str
    amount_cents: u256
    criteria_url: str
    evidence_url: str
    evidence_text: str
    status: str  # OPEN | REVIEWED | CLOSED
    impact: u256  # 0-25
    feasibility: u256  # 0-25
    clarity: u256  # 0-25
    budget: u256  # 0-25
    total_score: u256  # 0-100, deterministic sum of the four criteria
    verdict: str  # "" | strong | moderate | weak | reject
    rationale: str
    created_ts: u256
    reviewed_ts: u256


class GrantReview(gl.Contract):
    owner: Address
    proposal_count: u256
    proposals: TreeMap[str, Proposal]
    proposal_ids: DynArray[str]

    def __init__(self):
        self.owner = gl.message.sender_address
        self.proposal_count = u256(0)

    # ------------------------------------------------------------------
    # Helpers (deterministic)
    # ------------------------------------------------------------------
    def _now(self) -> u256:
        return u256(int(datetime.now(timezone.utc).timestamp()))

    def _get_proposal(self, proposal_id: str) -> Proposal:
        if proposal_id not in self.proposals:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Proposal not found: {proposal_id}"
            )
        return self.proposals[proposal_id]

    def _proposal_to_dict(self, p: Proposal) -> dict:
        return {
            "id": p.id,
            "proposer": p.proposer.as_hex,
            "title": p.title,
            "description": p.description,
            "amount_cents": int(p.amount_cents),
            "criteria_url": p.criteria_url,
            "evidence_url": p.evidence_url,
            "evidence_text": p.evidence_text,
            "status": p.status,
            "impact": int(p.impact),
            "feasibility": int(p.feasibility),
            "clarity": int(p.clarity),
            "budget": int(p.budget),
            "total_score": int(p.total_score),
            "verdict": p.verdict,
            "rationale": p.rationale,
            "created_ts": int(p.created_ts),
            "reviewed_ts": int(p.reviewed_ts),
        }

    def _fetch_page(self, url: str) -> str:
        """Fetch one URL, classifying failures for consensus."""
        res = gl.nondet.web.get(url)
        if res.status == 200:
            pass
        elif 400 <= res.status < 500:
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} URL returned {res.status}: {url}")
        else:
            raise gl.vm.UserError(f"{ERROR_TRANSIENT} URL returned {res.status}: {url}")
        try:
            return res.body.decode("utf-8", errors="ignore")[:EVIDENCE_CHARS]
        except Exception:
            raise gl.vm.UserError(f"{ERROR_TRANSIENT} Page undecodable: {url}")

    def _parse_scores(self, raw) -> dict:
        """Defensively parse the LLM scoring JSON (dict or raw string)."""
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            text = raw
            first = text.find("{")
            last = text.rfind("}")
            if first < 0 or last < 0:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM returned no JSON object")
            text = text[first : last + 1]
            text = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", text)
            try:
                data = json.loads(text)
            except Exception:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON unparsable")
        else:
            raise gl.vm.UserError(f"{ERROR_LLM} LLM returned non-dict: {type(raw)}")

        if not isinstance(data, dict):
            raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON is not an object")

        scores = {}
        for key in ("impact", "feasibility", "clarity", "budget"):
            value = data.get(key)
            if value is None:
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Missing '{key}'. Keys: {list(data.keys())}"
                )
            try:
                score = int(round(float(str(value).strip())))
            except (ValueError, TypeError):
                raise gl.vm.UserError(f"{ERROR_LLM} Non-numeric {key}: {value}")
            if score < 0 or score > MAX_CRITERION_SCORE:
                raise gl.vm.UserError(
                    f"{ERROR_LLM} {key} out of range 0-25: {score}"
                )
            scores[key] = score

        rationale = data.get("rationale", "")
        if not isinstance(rationale, str):
            rationale = str(rationale)
        scores["rationale"] = rationale[:500]
        # Total is derived by the contract, never trusted from the LLM.
        scores["total_score"] = (
            scores["impact"] + scores["feasibility"] + scores["clarity"] + scores["budget"]
        )
        scores["verdict"] = _verdict_band(scores["total_score"])
        return scores

    def _handle_leader_error(self, leaders_res, leader_fn) -> bool:
        """Canonical error comparison for validators (see write-contract skill)."""
        leader_msg = leaders_res.message if hasattr(leaders_res, "message") else ""
        try:
            leader_fn()
            return False  # Leader errored, validator succeeded - disagree
        except gl.vm.UserError as e:
            validator_msg = e.message if hasattr(e, "message") else str(e)
            if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(
                ERROR_EXTERNAL
            ):
                return validator_msg == leader_msg
            if validator_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(
                ERROR_TRANSIENT
            ):
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Register a grant proposal (anyone may register)
    # ------------------------------------------------------------------
    @gl.public.write
    def register_proposal(
        self,
        title: str,
        description: str,
        amount_cents: u256,
        criteria_url: str,
        evidence_url: str,
        evidence_text: str,
    ) -> str:
        title = title.strip()
        if len(title) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Title is empty")
        if len(title) > MAX_TITLE_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Title too long")

        description = description.strip()
        if len(description) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Description is empty")
        if len(description) > MAX_DESCRIPTION_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Description too long")

        if amount_cents <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Amount must be greater than 0")

        criteria_url = criteria_url.strip()
        if len(criteria_url) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Criteria URL is empty")
        if len(criteria_url) > MAX_URL_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Criteria URL too long")
        if not (criteria_url.startswith("http://") or criteria_url.startswith("https://")):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Criteria URL must be http(s)")

        evidence_url = evidence_url.strip()
        evidence_text = evidence_text.strip()
        if len(evidence_url) > MAX_URL_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence URL too long")
        if len(evidence_url) > 0 and not (
            evidence_url.startswith("http://") or evidence_url.startswith("https://")
        ):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence URL must be http(s)")
        if len(evidence_text) > MAX_EVIDENCE_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence text too long")

        proposal_id = f"prop_{int(self.proposal_count)}"
        proposal = Proposal(
            id=proposal_id,
            proposer=gl.message.sender_address,
            title=title,
            description=description,
            amount_cents=amount_cents,
            criteria_url=criteria_url,
            evidence_url=evidence_url,
            evidence_text=evidence_text,
            status="OPEN",
            impact=u256(0),
            feasibility=u256(0),
            clarity=u256(0),
            budget=u256(0),
            total_score=u256(0),
            verdict="",
            rationale="",
            created_ts=self._now(),
            reviewed_ts=u256(0),
        )
        self.proposals[proposal_id] = proposal
        self.proposal_ids.append(proposal_id)
        self.proposal_count = self.proposal_count + u256(1)
        return proposal_id

    # ------------------------------------------------------------------
    # Review a proposal by consensus scoring
    # ------------------------------------------------------------------
    @gl.public.write
    def review(self, proposal_id: str) -> dict:
        proposal = self._get_proposal(proposal_id)
        if proposal.status != "OPEN":
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Proposal is not open for review: {proposal.status}"
            )

        title = proposal.title
        description = proposal.description
        amount_cents = int(proposal.amount_cents)
        criteria_url = proposal.criteria_url
        url = proposal.evidence_url
        text = proposal.evidence_text

        def leader_fn() -> dict:
            criteria_page = self._fetch_page(criteria_url)
            evidence_page = ""
            if len(url) > 0:
                evidence_page = self._fetch_page(url)

            prompt = f"""You are scoring a grant proposal against the grant program's published criteria.
Proposal title: {title}
Proposal description: {description}
Requested amount: ${amount_cents / 100:.2f} (USD)
Scoring criteria fetched from {criteria_url}:
{criteria_page}
Evidence submitted by the proposer (text): {text}
Evidence page fetched from {url if len(url) > 0 else "(no evidence URL provided)"}:
{evidence_page}

Score the proposal using ONLY the criteria and evidence above. Do not
assume facts that are not in the evidence. Score each criterion 0-25:
- impact: expected benefit if the work succeeds
- feasibility: can this team realistically deliver it
- clarity: is the plan concrete, specific, and well scoped
- budget: is the requested amount reasonable for the work described
Respond with JSON only, nothing else:
{{"impact": <0-25>, "feasibility": <0-25>, "clarity": <0-25>, "budget": <0-25>, "rationale": "<one sentence quoting the decisive fact>", "confidence": <0-100>}}"""
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            return self._parse_scores(raw)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return self._handle_leader_error(leaders_res, leader_fn)
            try:
                mine = leader_fn()
            except gl.vm.UserError:
                return False
            except Exception:
                return False
            leader_data = leaders_res.calldata
            if not isinstance(leader_data, dict):
                return False
            # Agreement rule: total score within +/-10 points AND the same
            # verdict band. Free-text rationale may differ.
            leader_total = leader_data.get("total_score")
            my_total = mine.get("total_score")
            if leader_total is None or my_total is None:
                return False
            if abs(int(leader_total) - int(my_total)) > SCORE_TOLERANCE:
                return False
            return leader_data.get("verdict") == mine.get("verdict")

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        proposal.impact = u256(result["impact"])
        proposal.feasibility = u256(result["feasibility"])
        proposal.clarity = u256(result["clarity"])
        proposal.budget = u256(result["budget"])
        proposal.total_score = u256(result["total_score"])
        proposal.verdict = result["verdict"]
        proposal.rationale = result.get("rationale", "")
        proposal.status = "REVIEWED"
        proposal.reviewed_ts = self._now()
        self.proposals[proposal_id] = proposal
        return {
            "total_score": int(proposal.total_score),
            "verdict": proposal.verdict,
            "impact": int(proposal.impact),
            "feasibility": int(proposal.feasibility),
            "clarity": int(proposal.clarity),
            "budget": int(proposal.budget),
        }

    # ------------------------------------------------------------------
    # Contract owner closes/withdraws a proposal (OPEN or REVIEWED)
    # ------------------------------------------------------------------
    @gl.public.write
    def close_proposal(self, proposal_id: str) -> None:
        proposal = self._get_proposal(proposal_id)
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only the owner can close a proposal"
            )
        if proposal.status == "CLOSED":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Proposal is already closed")

        proposal.status = "CLOSED"
        self.proposals[proposal_id] = proposal

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @gl.public.view
    def get_proposal(self, proposal_id: str) -> dict:
        return self._proposal_to_dict(self._get_proposal(proposal_id))

    @gl.public.view
    def list_proposals(self) -> dict:
        return {
            "proposals": [
                self._proposal_to_dict(self.proposals[pid]) for pid in self.proposal_ids
            ]
        }

    @gl.public.view
    def get_proposal_count(self) -> u256:
        return self.proposal_count

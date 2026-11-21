"""Direct-mode tests for GrantReview.

Fast, in-memory (~30ms). Leader function only - validator logic is NOT
exercised here (see tests/integration for full consensus). All web/LLM
calls are mocked.
"""

import json

CRITERIA_URL = "https://example.com/grants/criteria"
EVIDENCE_URL = "https://example.com/evidence/milestone-plan"
TITLE = "Open-source indexer for GenLayer StudioNet"
DESCRIPTION = (
    "Build a public indexer that tracks every Intelligent Contract "
    "deployment on StudioNet with a free API and dashboard."
)
AMOUNT_CENTS = 500000  # $5,000.00


def _hex(addr) -> str:
    """Raw 20-byte test address -> 0x-prefixed hex (contract returns checksummed)."""
    return "0x" + bytes(addr).hex()


def _register(contract, direct_vm, proposer, evidence_text="Roadmap and team bios included."):
    direct_vm.sender = proposer
    return contract.register_proposal(
        TITLE, DESCRIPTION, AMOUNT_CENTS, CRITERIA_URL, EVIDENCE_URL, evidence_text
    )


def _mock_review(direct_vm, scores, criteria_page="Criteria: impact, feasibility, clarity, budget.",
                 evidence_page="Milestones and budget breakdown attached."):
    direct_vm.mock_web(
        r".*example\.com/grants/criteria.*",
        {"status": 200, "body": criteria_page},
    )
    direct_vm.mock_web(
        r".*example\.com/evidence/milestone-plan.*",
        {"status": 200, "body": evidence_page},
    )
    payload = dict(scores)
    payload.setdefault("rationale", "Decisive fact from the evidence.")
    payload.setdefault("confidence", 90)
    direct_vm.mock_llm(
        r".*scoring a grant proposal.*",
        json.dumps(payload),
    )


def test_register_proposal(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/grant_review.py")

    proposal_id = _register(contract, direct_vm, direct_alice)
    assert proposal_id == "prop_0"
    assert contract.get_proposal_count() == 1

    p = contract.get_proposal(proposal_id)
    assert p["proposer"].lower() == _hex(direct_alice).lower()
    assert p["title"] == TITLE
    assert p["description"] == DESCRIPTION
    assert p["amount_cents"] == AMOUNT_CENTS
    assert p["criteria_url"] == CRITERIA_URL
    assert p["evidence_url"] == EVIDENCE_URL
    assert p["status"] == "OPEN"
    assert p["total_score"] == 0
    assert p["verdict"] == ""
    assert p["rationale"] == ""
    assert p["reviewed_ts"] == 0

    listed = contract.list_proposals()
    assert len(listed["proposals"]) == 1
    assert listed["proposals"][0]["id"] == "prop_0"

    # A second proposer (bob) can register too - anyone may register
    proposal_id2 = _register(contract, direct_vm, direct_bob)
    assert proposal_id2 == "prop_1"
    assert contract.get_proposal_count() == 2
    assert contract.get_proposal("prop_1")["proposer"].lower() == _hex(direct_bob).lower()


def test_register_proposal_validation(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/grant_review.py")
    direct_vm.sender = direct_alice

    with direct_vm.expect_revert("[EXPECTED] Title is empty"):
        contract.register_proposal("   ", DESCRIPTION, AMOUNT_CENTS,
                                   CRITERIA_URL, EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Description is empty"):
        contract.register_proposal(TITLE, "   ", AMOUNT_CENTS,
                                   CRITERIA_URL, EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Amount must be greater than 0"):
        contract.register_proposal(TITLE, DESCRIPTION, 0,
                                   CRITERIA_URL, EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Criteria URL is empty"):
        contract.register_proposal(TITLE, DESCRIPTION, AMOUNT_CENTS,
                                   "", EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Criteria URL must be http(s)"):
        contract.register_proposal(TITLE, DESCRIPTION, AMOUNT_CENTS,
                                   "ftp://example.com/criteria", EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Evidence URL must be http(s)"):
        contract.register_proposal(TITLE, DESCRIPTION, AMOUNT_CENTS,
                                   CRITERIA_URL, "ftp://example.com/x", "")
    with direct_vm.expect_revert("[EXPECTED] Title too long"):
        contract.register_proposal("x" * 201, DESCRIPTION, AMOUNT_CENTS,
                                   CRITERIA_URL, EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Proposal not found: prop_99"):
        contract.get_proposal("prop_99")


def test_review_strong(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/grant_review.py")
    proposal_id = _register(contract, direct_vm, direct_alice)

    # Cannot review a proposal that does not exist
    with direct_vm.expect_revert("[EXPECTED] Proposal not found: prop_99"):
        contract.review("prop_99")

    _mock_review(direct_vm, {"impact": 22, "feasibility": 21, "clarity": 20, "budget": 19})
    result = contract.review(proposal_id)
    assert result["total_score"] == 82
    assert result["verdict"] == "strong"
    assert result["impact"] == 22
    assert result["budget"] == 19
    p = contract.get_proposal(proposal_id)
    assert p["status"] == "REVIEWED"
    assert p["total_score"] == 82
    assert p["verdict"] == "strong"
    assert p["rationale"] == "Decisive fact from the evidence."
    assert p["reviewed_ts"] > 0

    # Reviewing twice reverts
    with direct_vm.expect_revert("[EXPECTED] Proposal is not open for review"):
        contract.review(proposal_id)


def test_review_verdict_bands(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/grant_review.py")
    prop0 = _register(contract, direct_vm, direct_alice)
    prop1 = _register(contract, direct_vm, direct_bob)

    # 60-79 -> moderate
    _mock_review(direct_vm, {"impact": 18, "feasibility": 17, "clarity": 16, "budget": 15})
    result = contract.review(prop0)
    assert result["total_score"] == 66
    assert result["verdict"] == "moderate"

    # 0-39 -> reject
    direct_vm.clear_mocks()
    _mock_review(direct_vm, {"impact": 5, "feasibility": 8, "clarity": 6, "budget": 7},
                 evidence_page="No plan, no team, no budget breakdown.")
    result = contract.review(prop1)
    assert result["total_score"] == 26
    assert result["verdict"] == "reject"
    assert contract.get_proposal(prop1)["status"] == "REVIEWED"


def test_review_weak_band_and_text_only_evidence(direct_vm, direct_deploy, direct_alice):
    """Proposal with no evidence URL: scoring comes from the text alone; 40-59 -> weak."""
    contract = direct_deploy("contracts/grant_review.py")
    direct_vm.sender = direct_alice
    proposal_id = contract.register_proposal(
        TITLE, DESCRIPTION, AMOUNT_CENTS, CRITERIA_URL, "", "Full plan text here."
    )

    direct_vm.mock_web(
        r".*example\.com/grants/criteria.*",
        {"status": 200, "body": "Criteria: impact, feasibility, clarity, budget."},
    )
    direct_vm.mock_llm(
        r".*scoring a grant proposal.*",
        json.dumps({"impact": 12, "feasibility": 13, "clarity": 11, "budget": 12,
                    "rationale": "Text shows a thin plan.", "confidence": 70}),
    )

    result = contract.review(proposal_id)
    assert result["total_score"] == 48
    assert result["verdict"] == "weak"
    assert contract.get_proposal(proposal_id)["status"] == "REVIEWED"


def test_review_llm_validation(direct_vm, direct_deploy, direct_alice):
    """Out-of-range criterion scores and missing keys are LLM errors."""
    contract = direct_deploy("contracts/grant_review.py")
    proposal_id = _register(contract, direct_vm, direct_alice)

    direct_vm.mock_web(
        r".*example\.com/grants/criteria.*",
        {"status": 200, "body": "Criteria page."},
    )
    direct_vm.mock_web(
        r".*example\.com/evidence/milestone-plan.*",
        {"status": 200, "body": "Evidence page."},
    )
    direct_vm.mock_llm(
        r".*scoring a grant proposal.*",
        json.dumps({"impact": 90, "feasibility": 21, "clarity": 20, "budget": 19,
                    "rationale": "Score on a 0-100 scale by mistake.", "confidence": 90}),
    )
    with direct_vm.expect_revert("[LLM_ERROR] impact out of range 0-25"):
        contract.review(proposal_id)

    direct_vm.clear_mocks()
    _mock_review(direct_vm, {"impact": 20, "feasibility": 20, "clarity": 20})
    with direct_vm.expect_revert("[LLM_ERROR] Missing 'budget'"):
        contract.review(proposal_id)


def test_close_proposal(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    contract = direct_deploy("contracts/grant_review.py")
    proposal_id = _register(contract, direct_vm, direct_alice)

    # The proposer (alice) is NOT the contract owner here (deployer is the
    # default test account), so closing as alice must revert
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] Only the owner can close a proposal"):
        contract.close_proposal(proposal_id)

    # Owner closes an OPEN proposal
    direct_vm.sender = direct_owner
    contract.close_proposal(proposal_id)
    assert contract.get_proposal(proposal_id)["status"] == "CLOSED"

    # Cannot close twice
    with direct_vm.expect_revert("[EXPECTED] Proposal is already closed"):
        contract.close_proposal(proposal_id)

    # A closed proposal cannot be reviewed
    with direct_vm.expect_revert("[EXPECTED] Proposal is not open for review"):
        contract.review(proposal_id)


def test_close_after_review(direct_vm, direct_deploy, direct_alice, direct_owner):
    contract = direct_deploy("contracts/grant_review.py")
    proposal_id = _register(contract, direct_vm, direct_alice)
    _mock_review(direct_vm, {"impact": 15, "feasibility": 14, "clarity": 13, "budget": 12})
    result = contract.review(proposal_id)
    assert result["total_score"] == 54
    assert result["verdict"] == "weak"

    direct_vm.sender = direct_owner
    contract.close_proposal(proposal_id)
    p = contract.get_proposal(proposal_id)
    assert p["status"] == "CLOSED"
    assert p["total_score"] == 54  # review result is kept after closing
    assert p["verdict"] == "weak"

"""Integration tests for GrantReview against GLSim (localnet).

Full consensus: leader + validators run the review with mocked web/LLM
responses (via transaction_context), so no real LLM API key is needed.

Requires a running simulator: glsim --port 4000 --validators 5
Run: gltest tests/integration/ -v -s --network localnet
"""

import json
import urllib.request

from gltest import create_accounts, get_contract_factory
from gltest.assertions import tx_execution_failed, tx_execution_succeeded

CRITERIA_URL = "https://example.com/grants/criteria"
EVIDENCE_URL = "https://example.com/evidence/milestone-plan"
TITLE = "Open-source indexer for GenLayer StudioNet"
DESCRIPTION = (
    "Build a public indexer that tracks every Intelligent Contract "
    "deployment on StudioNet with a free API and dashboard."
)
AMOUNT_CENTS = 500000  # $5,000.00
GLSIM_URL = "http://127.0.0.1:4000/api"


def _rpc(method, params):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        GLSIM_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    if "error" in payload:
        raise RuntimeError(f"GLSim RPC {method} failed: {payload['error']}")
    return payload["result"]


def _review_context(scores):
    """Mocks for review: criteria page + evidence page + LLM scores."""
    payload = dict(scores)
    payload.setdefault("rationale", "Decisive fact from the evidence.")
    payload.setdefault("confidence", 90)
    return {
        "validators": [
            {
                "plugin_config": {
                    "mock_web_response": {
                        "nondet_web_request": {
                            CRITERIA_URL: {
                                "status": 200,
                                "body": "Criteria: impact, feasibility, clarity, budget.",
                            },
                            EVIDENCE_URL: {
                                "status": 200,
                                "body": "Milestones and budget breakdown attached.",
                            },
                        }
                    },
                    "mock_response": {
                        "response": {
                            ".*scoring a grant proposal.*": json.dumps(payload)
                        }
                    },
                }
            }
        ],
    }


def _deploy():
    factory = get_contract_factory("GrantReview")
    contract = factory.deploy(args=[])
    alice, bob = create_accounts(2)
    return contract, alice, bob


def _register(contract, reporter):
    receipt = contract.connect(reporter).register_proposal(
        args=[TITLE, DESCRIPTION, AMOUNT_CENTS, CRITERIA_URL, EVIDENCE_URL,
              "Roadmap and team bios included."]
    ).transact()
    assert tx_execution_succeeded(receipt)
    return "prop_0"


def test_full_flow_register_review_strong():
    contract, alice, bob = _deploy()
    proposal_id = _register(contract, alice)

    proposal = contract.get_proposal(args=[proposal_id]).call()
    assert proposal["status"] == "OPEN"
    assert contract.get_proposal_count(args=[]).call() == 1

    # Review with mocked criteria/evidence pages + LLM scores (full consensus)
    receipt = contract.review(args=[proposal_id]).transact(
        transaction_context=_review_context(
            {"impact": 22, "feasibility": 21, "clarity": 20, "budget": 19}
        )
    )
    assert tx_execution_succeeded(receipt)
    proposal = contract.get_proposal(args=[proposal_id]).call()
    assert proposal["status"] == "REVIEWED"
    assert proposal["total_score"] == 82
    assert proposal["verdict"] == "strong"
    assert proposal["impact"] == 22
    assert proposal["budget"] == 19
    assert proposal["rationale"] == "Decisive fact from the evidence."


def test_full_flow_review_reject():
    contract, alice, bob = _deploy()
    proposal_id = _register(contract, alice)

    receipt = contract.review(args=[proposal_id]).transact(
        transaction_context=_review_context(
            {"impact": 5, "feasibility": 8, "clarity": 6, "budget": 7}
        )
    )
    assert tx_execution_succeeded(receipt)
    proposal = contract.get_proposal(args=[proposal_id]).call()
    assert proposal["status"] == "REVIEWED"
    assert proposal["total_score"] == 26
    assert proposal["verdict"] == "reject"


def test_close_proposal_owner_only_and_review_closed_fails():
    contract, alice, bob = _deploy()
    proposal_id = _register(contract, alice)

    # Proposer (alice) is not the contract owner - close must fail
    receipt = contract.connect(alice).close_proposal(args=[proposal_id]).transact()
    assert tx_execution_failed(receipt)
    assert contract.get_proposal(args=[proposal_id]).call()["status"] == "OPEN"

    # Owner (default account) closes the proposal
    receipt = contract.close_proposal(args=[proposal_id]).transact()
    assert tx_execution_succeeded(receipt)
    assert contract.get_proposal(args=[proposal_id]).call()["status"] == "CLOSED"

    # A closed proposal cannot be reviewed
    receipt = contract.review(args=[proposal_id]).transact(
        transaction_context=_review_context(
            {"impact": 20, "feasibility": 20, "clarity": 20, "budget": 20}
        )
    )
    assert tx_execution_failed(receipt)


def test_review_open_only():
    contract, alice, bob = _deploy()

    # Reviewing a proposal that was never registered fails
    receipt = contract.review(args=["prop_99"]).transact(
        transaction_context=_review_context(
            {"impact": 20, "feasibility": 20, "clarity": 20, "budget": 20}
        )
    )
    assert tx_execution_failed(receipt)

"""Decision model client and trajectory evaluator for agent steering.

Connects to a Clef-Flash / Jev / SystemOne endpoint (or via llama-swap upstream)
to evaluate agent trajectory health and recommend loop-level steering actions
in a single forward pass without free-form text generation.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

SYSTEMONE_SCHEMA_QUESTIONS = {
    "trajectory": {
        "type": "choice",
        "instructions": "Evaluate the health of the agent execution trajectory.",
        "criteria": {
            "healthy": "Agent is executing valid tool calls and making forward progress.",
            "spinning": "Agent is repeating identical tool calls or stalled without productive changes.",
            "failing": "Agent is stuck encountering recurring gate/test failures or syntax errors.",
        },
    },
    "recommended_action": {
        "type": "choice",
        "instructions": "Recommended loop controller action.",
        "criteria": {
            "continue": "Allow the agent to proceed normally.",
            "steer": "Inject a steering prompt to guide the agent out of a suboptimal path.",
            "interrupt": "Halt the iteration early to avoid wasted compute and context thrash.",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "Rate how urgently an intervention is needed (0=none, 1=moderate, 2=immediate).",
        "criteria": ["No intervention", "Moderate guidance suggested", "Immediate halt required"],
    },
}

REVIEW_SCHEMA_QUESTIONS = {
    "review_verdict": {
        "type": "choice",
        "instructions": "Review this implementation. Does it satisfy all requirements and invariants?",
        "criteria": {
            "approved": "The changes meet the objective, all tests and gates pass, and invariants are preserved.",
            "changes_requested": "The changes have defects, fail requirements, break tests, or introduce regressions.",
        },
    },
    "code_quality": {
        "type": "choice",
        "instructions": "Assess overall code and architectural quality.",
        "criteria": {
            "clean": "Code is well-structured, modular, and follows project conventions.",
            "smelly": "Code contains anti-patterns, brittle hacks, or improper modularization.",
        },
    },
}


@dataclass(frozen=True)
class DecisionResult:
    trajectory: str
    recommended_action: str
    urgency: float
    confidence: float
    raw_answers: dict[str, Any]


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    code_quality: str
    confidence: float
    probabilities: dict[str, float]
    raw_answers: dict[str, Any]


def build_systemone_request(
    state: dict[str, Any],
    model: str = "clef-flash",
) -> dict[str, Any]:
    """Format agent execution state into a SystemOne request body."""
    return {
        "model": model,
        "state": state,
        "questions": SYSTEMONE_SCHEMA_QUESTIONS,
    }


def parse_systemone_response(data: dict[str, Any]) -> DecisionResult:
    """Parse a SystemOne response payload into a DecisionResult."""
    answers = data.get("answers", {})

    traj_ans = answers.get("trajectory", {})
    trajectory = traj_ans.get("choice", "healthy")

    action_ans = answers.get("recommended_action", {})
    action = action_ans.get("choice", "continue")
    conf = action_ans.get("confidence", 1.0)

    urgency_ans = answers.get("urgency", {})
    urgency = float(urgency_ans.get("score", 0.0))

    return DecisionResult(
        trajectory=trajectory,
        recommended_action=action,
        urgency=urgency,
        confidence=conf,
        raw_answers=answers,
    )


def build_review_request(
    state: dict[str, Any],
    model: str = "clef-flash",
) -> dict[str, Any]:
    """Format review context into a SystemOne review request body."""
    return {
        "model": model,
        "state": state,
        "questions": REVIEW_SCHEMA_QUESTIONS,
    }


def parse_review_response(data: dict[str, Any]) -> ReviewResult:
    """Parse a SystemOne response payload into a ReviewResult."""
    answers = data.get("answers", {})

    verd_ans = answers.get("review_verdict", {})
    verdict = verd_ans.get("choice", "approved").upper()
    conf = verd_ans.get("confidence", 1.0)
    probs = verd_ans.get("probabilities", {})

    qual_ans = answers.get("code_quality", {})
    quality = qual_ans.get("choice", "clean")

    return ReviewResult(
        verdict=verdict,
        code_quality=quality,
        confidence=conf,
        probabilities=probs,
        raw_answers=answers,
    )


def evaluate_trajectory(
    endpoint_url: str,
    state: dict[str, Any],
    model: str = "clef-flash",
    timeout: float = 180.0,
) -> DecisionResult | None:
    """Query the decision model endpoint, returning None on network or format errors.

    ponytail: synchronous urllib call with CPU-safe timeout; upgrade to async or background thread
    if evaluation frequency exceeds once per tool call.
    """
    payload = json.dumps(build_systemone_request(state, model=model)).encode("utf-8")
    req = urllib.request.Request(
        endpoint_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
            return parse_systemone_response(body)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
        return None


def evaluate_review(
    endpoint_url: str,
    state: dict[str, Any],
    model: str = "clef-flash",
    timeout: float = 180.0,
) -> ReviewResult | None:
    """Query the decision model for a formal review verdict."""
    payload = json.dumps(build_review_request(state, model=model)).encode("utf-8")
    req = urllib.request.Request(
        endpoint_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
            return parse_review_response(body)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
        return None


def steer_prompt(decision: DecisionResult, state: dict[str, Any]) -> str:
    """Generate guidance text based on the decision model's assessment."""
    if decision.recommended_action == "continue":
        return ""
    if decision.trajectory == "spinning":
        repeated = state.get("repeated", "")
        detail = f" on '{repeated}'" if repeated else ""
        return (
            f"Steering notice: You appear to be repeating tool calls{detail} without making progress. "
            "Step back, review the error or file contents carefully, and adopt an alternative approach."
        )
    if decision.trajectory == "failing":
        return (
            "Steering notice: Recent edits are causing recurring check/test failures. "
            "Inspect the test failure diff, isolate the breaking change, and fix the root cause before proceeding."
        )
    return "Steering notice: Course correction recommended. Check current diff against the plan."

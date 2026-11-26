"""Conservative extraction of attributed claims from historical source text."""

import re

from cognicore.experience.schema import Attempt, AttemptOutcome

KINDS = ("decision", "attempt", "failure", "success", "proposal", "milestone", "procedure")

_DECISION = re.compile(
    r"^(?:(?:we|i|the team) )?(?:decided to|chose|agreed to|opted for|selected|settled on) (.+)$", re.I
)
_FAILURE = re.compile(r"^(.{2,160}?)\s+(?:failed|did not (?:work|pass)|didn't (?:work|pass)|broke)\b(.*)$", re.I)
_SUCCESS = re.compile(r"^(.{2,160}?)\s+(?:worked|succeeded|passed|fixed the issue)\b(.*)$", re.I)
_ATTEMPT = re.compile(r"^(?:(?:we|i|the team) )?(?:tried|attempted|tested) (.+)$", re.I)
_PROPOSAL = re.compile(r"^(?:(?:we|i|the team) )?(?:proposed|suggested|recommended) (.+)$", re.I)
_MILESTONE = re.compile(
    r"^(?:(?:we|i|the team) )?(?:created|launched|released|shipped|migrated|switched|abandoned|"
    r"deprecated|adopted|completed|still need to|still needs) (.+)$", re.I
)
_PROCEDURE = re.compile(r"^(?:run|execute|install|configure|set|add|use|try|step \d+[.):])\s+(.+)$", re.I)
_REASON = re.compile(r"\s+(?:because|due to)\s+", re.I)
_ALTERNATIVE = re.compile(r"\s+(?:instead of|rather than|over)\s+", re.I)
_ENVIRONMENT = re.compile(r"\s+(?:on|under|with|when)\s+(.+)$", re.I)


def _parts(pattern, text):
    parts = pattern.split(text, maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else None


def extract(message):
    """Emit source excerpts and explicit fields. A claimed outcome remains a claim."""
    if message["role"] not in {"user", "assistant", "tool"}:
        return
    in_code = False
    for line in message["text"].splitlines():
        line = line.strip()
        if len(line) > 4096:
            continue
        if line.startswith(("\x60\x60\x60", "~~~")):
            in_code = not in_code
            continue
        statements = [line] if in_code else re.split(r"(?<=[.!?])\s+", line)
        for statement in statements:
            excerpt = statement.strip().lstrip("-* ").strip()
            if not excerpt or excerpt.endswith("?"):
                continue
            text = excerpt.rstrip(".!")
            decision = None if in_code else _DECISION.fullmatch(text)
            failure = None if in_code else _FAILURE.fullmatch(text)
            success = None if in_code else _SUCCESS.fullmatch(text)
            attempt_match = None if in_code else _ATTEMPT.fullmatch(text)
            proposal = None if in_code else _PROPOSAL.fullmatch(text)
            kind, subject = None, None
            fields = {
                "objective": None,
                "decision": None,
                "reason": None,
                "alternatives": None,
                "attempt": None,
                "environment": None,
                "solution": None,
                "executed": None,
                "outcome_confirmed": None,
                "reusable": None,
                "procedure": None,
                "milestone": None,
            }
            if decision:
                choice, fields["reason"] = _parts(_REASON, decision[1])
                choice, alternative = _parts(_ALTERNATIVE, choice)
                fields["decision"] = choice
                fields["alternatives"] = [alternative] if alternative else None
                kind, subject = "decision", choice
            elif failure or success:
                claim = failure or success
                approach, reason = _parts(_REASON, claim[1] + claim[2])
                environment = _ENVIRONMENT.search(approach)
                if environment:
                    fields["environment"] = environment[1]
                    approach = approach[:environment.start()]
                outcome = AttemptOutcome.FAILURE if failure else AttemptOutcome.SUCCESS
                attempt = Attempt(approach.strip(), outcome.value, reason or "").to_dict()
                attempt["reason"] = reason
                attempt["evidence"] = None
                fields["attempt"] = attempt
                fields["reason"] = reason
                fields["solution"] = None if failure else attempt["approach"]
                kind, subject = ("failure" if failure else "success"), attempt["approach"]
            elif attempt_match:
                approach, reason = _parts(_REASON, attempt_match[1])
                fields["attempt"] = {**Attempt(approach).to_dict(), "reason": reason, "evidence": None}
                fields["reason"] = reason
                kind, subject = "attempt", approach
            elif proposal:
                fields["solution"], fields["reason"] = _parts(_REASON, proposal[1])
                kind, subject = "proposal", fields["solution"]
            elif not in_code and _MILESTONE.fullmatch(text):
                kind, subject = "milestone", text
                fields["milestone"], fields["reason"] = _parts(_REASON, text)
            elif in_code or _PROCEDURE.fullmatch(text):
                kind, subject = "procedure", text
                fields["procedure"] = excerpt
            if kind:
                yield {
                    "kind": kind,
                    "text": excerpt,
                    "fields": fields,
                    "subject": " ".join(re.findall(r"\w+", subject.casefold())),
                    "extractor": "rules-v1",
                }

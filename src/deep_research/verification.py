from __future__ import annotations

import re
from typing import Any


_CLAIM_BOUNDARY = re.compile(r"(?<=[.!?。！？;；])\s*|\n+")
_CITATION = re.compile(r"\[(E[0-9a-f]+)\]")


def _is_answer_scope_note(claim: str) -> bool:
    """Keep editorial answer framing out of the evidence-claim contract."""

    normalized = " ".join(claim.casefold().split())
    return normalized.startswith(
        (
            "本回答",
            "本文",
            "以下回答",
            "下文",
            "this answer",
            "the answer below",
        )
    )


def parse_answer_claims(answer: str) -> list[dict[str, Any]]:
    segments = [value.strip() for value in _CLAIM_BOUNDARY.split(answer) if value.strip()]
    claims: list[dict[str, Any]] = []
    for segment in segments:
        evidence_ids = _CITATION.findall(segment)
        claim = _CITATION.sub("", segment)
        claim = re.sub(r"\s+([.!?。！？;；])", r"\1", claim).strip()
        substantive = re.sub(r"[.!?。！？;；\s]+", "", claim)
        if evidence_ids and not substantive and claims:
            claims[-1]["evidence_ids"] = list(
                dict.fromkeys([*claims[-1]["evidence_ids"], *evidence_ids])
            )
            continue
        if not substantive:
            continue
        if not evidence_ids and _is_answer_scope_note(claim):
            continue
        claims.append(
            {
                "claim_id": "",
                "claim": claim,
                "evidence_ids": evidence_ids,
            }
        )
    for index, claim in enumerate(claims, start=1):
        claim["claim_id"] = f"C{index}"
    return claims

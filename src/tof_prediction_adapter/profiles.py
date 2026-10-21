"""Versioned advisory profiles; no Job lifecycle or authorization side effects."""

PROFILES = {
    "research.evidence_screen.v1": {
        "description": "Prioritize candidate evidence for human or application verification.",
        "questions": {
            "relevance": {
                "type": "score",
                "instructions": "How directly does this source address the supplied claim?",
                "criteria": ["Irrelevant", "Indirectly related", "Directly relevant"],
            },
            "independent_evidence": {
                "type": "noul",
                "instructions": "Does this source present independent evidence rather than repeat another source?",
            },
            "needs_verification": {
                "type": "noul",
                "instructions": "Does the source contain unsupported or conflicting claims requiring further verification?",
            },
        },
    },
    "provider.progress.v1": {
        "description": "Recommend investigation of provider progress without stopping or completing Jobs.",
        "questions": {
            "progress": {
                "type": "choice",
                "instructions": "Classify the recent provider activity based on observable results.",
                "criteria": {
                    "progress": "New useful work or verified results",
                    "repetition": "Repeating prior actions without new results",
                    "blocked": "An explicit dependency prevents progress",
                    "insufficient_context": "Cannot assess from the supplied activity",
                },
            },
        },
    },
    "job.complexity.v1": {
        "description": "Assess workload complexity for advisory provider selection.",
        "questions": {
            "complexity": {
                "type": "score",
                "instructions": "Assess the complexity of the supplied task.",
                "criteria": [
                    "Simple extraction or classification",
                    "Moderate analysis",
                    "Complex multi-step synthesis",
                ],
            },
            "needs_vision": {
                "type": "noul",
                "instructions": "Does this task require interpreting images or visual documents?",
            },
        },
    },
}

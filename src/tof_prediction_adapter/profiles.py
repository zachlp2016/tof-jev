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
    "security.data_readiness.v1": {
        "description": "Assess whether retained security inputs support downstream analysis.",
        "questions": {
            "identity_quality": {
                "type": "score",
                "instructions": "Assess the quality of the resolved security identity and listing mapping.",
                "criteria": ["Unresolved", "Partially supported", "Authoritatively supported"],
            },
            "history_usable": {
                "type": "noul",
                "instructions": "Is the retained price and return history usable for the stated analysis capabilities?",
            },
            "critical_input_stale": {
                "type": "noul",
                "instructions": "Is any critical retained input stale relative to its supplied source date?",
            },
            "options_usable": {
                "type": "noul",
                "instructions": "Are the retained option quotes and coverage usable for option analysis?",
            },
            "volatility_ready": {
                "type": "noul",
                "instructions": "Do the supplied inputs support the deterministic volatility calculations?",
            },
            "missing_input_priority": {
                "type": "choice",
                "instructions": "Which missing or deficient input should be addressed first?",
                "criteria": {
                    "identity": "Security or listing identity",
                    "history": "Price or return history",
                    "calendar": "Trading or contract calendar",
                    "rates": "Risk-free rate curve",
                    "dividends": "Dividend or yield inputs",
                    "options": "Option contracts or quotes",
                    "none": "No material input deficiency",
                },
            },
            "source_conflict": {
                "type": "noul",
                "instructions": "Do the supplied source records materially conflict with one another?",
            },
            "source_traceability": {
                "type": "score",
                "instructions": "Assess whether material fields can be traced to dated source evidence.",
                "criteria": ["Insufficient", "Partial", "Complete"],
            },
            "readiness_level": {
                "type": "score",
                "instructions": "Assess overall readiness for downstream security analysis.",
                "criteria": ["Blocked", "Partial", "Ready"],
            },
            "manual_review": {
                "type": "noul",
                "instructions": "Does this security record require human review before relying on it?",
            },
        },
    },
    "volatility.interpretation.v1": {
        "description": "Interpret deterministic realized, conditional, and implied volatility outputs.",
        "questions": {
            "volatility_level": {
                "type": "score",
                "instructions": "Assess the supplied security's overall volatility level; higher levels mean more volatility.",
                "criteria": ["Low", "Moderate", "Elevated", "High", "Extreme"],
            },
            "estimates_consistent": {
                "type": "noul",
                "instructions": "Are the supplied historical, EWMA, EGARCH, and implied estimates directionally consistent?",
            },
            "regime_shift_signal": {
                "type": "noul",
                "instructions": "Do differences among recent and long-window estimates indicate a possible volatility regime shift?",
            },
            "implied_relative_level": {
                "type": "score",
                "instructions": "Assess implied volatility relative to the supplied realized and conditional estimates.",
                "criteria": ["Discounted", "Aligned", "Premium", "Large premium"],
            },
            "surface_usable": {
                "type": "noul",
                "instructions": "Is the supplied volatility surface sufficiently supported for interpretation?",
            },
            "option_data_deficient": {
                "type": "noul",
                "instructions": "Are stale, sparse, crossed, or otherwise deficient option observations affecting interpretation?",
            },
            "preferred_measure": {
                "type": "choice",
                "instructions": "Which supplied volatility family is most informative for near-term risk interpretation?",
                "criteria": {
                    "historical": "Fixed-window historical volatility",
                    "ewma": "Exponentially weighted volatility",
                    "egarch": "Asymmetric conditional volatility",
                    "implied": "Option-implied volatility",
                    "insufficient": "No supplied measure is sufficiently reliable",
                },
            },
            "manual_review": {
                "type": "noul",
                "instructions": "Does the volatility record require human review before portfolio use?",
            },
        },
    },
    "portfolio.risk_review.v1": {
        "description": "Review deterministic portfolio-risk results and data limitations.",
        "questions": {
            "concentration_level": {
                "type": "score",
                "instructions": "Assess portfolio concentration from the supplied position weights and accounts.",
                "criteria": ["Low", "Moderate", "Elevated", "High", "Extreme"],
            },
            "diversification_quality": {
                "type": "score",
                "instructions": "Assess diversification after considering supplied correlations and common exposures.",
                "criteria": ["Poor", "Limited", "Moderate", "Strong"],
            },
            "volatility_level": {
                "type": "score",
                "instructions": "Assess the supplied portfolio volatility level; higher levels mean more volatility.",
                "criteria": ["Low", "Moderate", "Elevated", "High", "Extreme"],
            },
            "hidden_common_exposure": {
                "type": "noul",
                "instructions": "Do nominally different positions appear to share a material common exposure?",
            },
            "liquidity_concern": {
                "type": "noul",
                "instructions": "Do the supplied holdings or market-data warnings indicate a material liquidity concern?",
            },
            "unmodeled_weight_material": {
                "type": "noul",
                "instructions": "Is the excluded or unmodeled portfolio weight material to interpreting the result?",
            },
            "account_risk_dispersion": {
                "type": "score",
                "instructions": "Assess how unevenly risk is distributed across the supplied child accounts.",
                "criteria": ["Similar", "Moderately uneven", "Highly uneven"],
            },
            "dominant_risk_source": {
                "type": "choice",
                "instructions": "Which supplied factor is the dominant portfolio-risk concern?",
                "criteria": {
                    "single_security": "One security or issuer",
                    "correlation": "Correlated positions or common factor exposure",
                    "options": "Options or nonlinear exposure",
                    "leverage": "Gross exposure, shorts, or leverage",
                    "liquidity": "Liquidity or tradability",
                    "data_gap": "Missing or unreliable inputs",
                    "none": "No dominant concern in the supplied state",
                },
            },
            "review_priority": {
                "type": "score",
                "instructions": "Prioritize this portfolio for risk review.",
                "criteria": ["Routine", "Soon", "Immediate"],
            },
            "manual_review": {
                "type": "noul",
                "instructions": "Does this portfolio require human review before relying on the risk summary?",
            },
        },
    },
    "market.event_impact.v1": {
        "description": "Assess a retained market event's potential impact on a named security or portfolio.",
        "questions": {
            "relevance": {
                "type": "score",
                "instructions": "How directly is the supplied event relevant to the named target?",
                "criteria": ["Irrelevant", "Indirect", "Material", "Direct"],
            },
            "direction": {
                "type": "choice",
                "instructions": "What directional effect does the supplied evidence support for the named target?",
                "criteria": {
                    "positive": "Predominantly positive",
                    "negative": "Predominantly negative",
                    "mixed": "Material positive and negative channels",
                    "unclear": "Direction is not supported by the supplied evidence",
                },
            },
            "magnitude": {
                "type": "score",
                "instructions": "Assess the potential impact magnitude from the supplied evidence.",
                "criteria": ["Negligible", "Small", "Moderate", "Large"],
            },
            "horizon": {
                "type": "choice",
                "instructions": "Which impact horizon is best supported?",
                "criteria": {
                    "intraday": "Within the trading day",
                    "days": "Several trading days",
                    "weeks": "Several weeks",
                    "months": "Several months",
                    "long_term": "Longer than several months",
                    "unclear": "No horizon is supported",
                },
            },
            "transmission_channel": {
                "type": "choice",
                "instructions": "Which channel most directly transmits the event to the target?",
                "criteria": {
                    "earnings": "Revenue, costs, margins, or earnings",
                    "rates_credit": "Interest rates, credit, or funding",
                    "liquidity": "Market or balance-sheet liquidity",
                    "regulation": "Law, regulation, litigation, or policy",
                    "commodity_fx": "Commodity or foreign-exchange exposure",
                    "sentiment": "Positioning, attention, or sentiment",
                    "other": "Another evidenced channel",
                    "unclear": "No channel is supported",
                },
            },
            "evidence_sufficient": {
                "type": "noul",
                "instructions": "Is the supplied evidence sufficient to support the impact assessment?",
            },
            "needs_independent_confirmation": {
                "type": "noul",
                "instructions": "Does the event require confirmation from an independent source?",
            },
            "monitoring_priority": {
                "type": "score",
                "instructions": "Prioritize this event for continued monitoring.",
                "criteria": ["Low", "Routine", "High", "Immediate"],
            },
        },
    },
    "scenario.assessment.v1": {
        "description": "Compare predefined market scenarios without claiming calibrated probabilities.",
        "questions": {
            "best_supported_scenario": {
                "type": "choice",
                "instructions": "Which predefined directional scenario is best supported by the supplied state?",
                "criteria": {
                    "bullish": "The supplied bullish scenario",
                    "base": "The supplied base or neutral scenario",
                    "bearish": "The supplied bearish scenario",
                    "insufficient": "The supplied evidence does not support a scenario choice",
                },
            },
            "evidence_strength": {
                "type": "score",
                "instructions": "Assess the strength of evidence distinguishing the scenarios.",
                "criteria": ["Weak", "Limited", "Moderate", "Strong"],
            },
            "uncertainty_level": {
                "type": "score",
                "instructions": "Assess uncertainty remaining after considering the supplied evidence.",
                "criteria": ["Low", "Moderate", "High", "Extreme"],
            },
            "assumption_stability": {
                "type": "score",
                "instructions": "Assess the stability of the key assumptions underlying the leading scenario.",
                "criteria": ["Fragile", "Mixed", "Robust"],
            },
            "disconfirming_evidence": {
                "type": "noul",
                "instructions": "Is material evidence present that conflicts with the leading scenario?",
            },
            "tail_risk_material": {
                "type": "noul",
                "instructions": "Is a supplied low-frequency adverse outcome material enough to retain explicitly?",
            },
            "needs_more_evidence": {
                "type": "noul",
                "instructions": "Is additional evidence required before using the scenario assessment?",
            },
            "review_priority": {
                "type": "score",
                "instructions": "Prioritize this scenario set for human review.",
                "criteria": ["Routine", "Soon", "Immediate"],
            },
        },
    },
}

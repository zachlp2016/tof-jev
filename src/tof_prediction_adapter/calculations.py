"""Bounded, deterministic arithmetic for advisory decision support.

These operations transform caller-supplied numbers. They do not estimate inputs,
fit a model, establish causation, or authorize a TOF workflow action.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Probability = Annotated[FiniteFloat, Field(ge=0, le=1)]
NonnegativeInteger = Annotated[int, Field(strict=True, ge=0)]
PROBABILITY_SUM_TOLERANCE = 1e-9


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RatioRequest(StrictModel):
    operation: Literal["ratio"]
    numerator: FiniteFloat
    denominator: FiniteFloat

    @model_validator(mode="after")
    def nonzero_denominator(self) -> RatioRequest:
        if self.denominator == 0:
            raise ValueError("denominator must be nonzero")
        return self


class BayesRequest(StrictModel):
    operation: Literal["bayes"]
    prior: Probability
    sensitivity: Probability
    false_positive_rate: Probability


class Outcome(StrictModel):
    value: FiniteFloat
    probability: Probability


class ExpectedValueRequest(StrictModel):
    operation: Literal["expected_value"]
    outcomes: Annotated[list[Outcome], Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> ExpectedValueRequest:
        total = math.fsum(outcome.probability for outcome in self.outcomes)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=PROBABILITY_SUM_TOLERANCE):
            raise ValueError("outcome probabilities must sum to 1 (absolute tolerance 1e-9)")
        return self


class NormalCDFRequest(StrictModel):
    operation: Literal["normal_cdf"]
    x: FiniteFloat
    mean: FiniteFloat = 0.0
    standard_deviation: Annotated[FiniteFloat, Field(gt=0)] = 1.0


class BinomialRequest(StrictModel):
    operation: Literal["binomial"]
    n: Annotated[NonnegativeInteger, Field(le=10000)]
    k: NonnegativeInteger
    probability: Probability
    mode: Literal["exact", "at_most", "at_least"] = "exact"

    @model_validator(mode="after")
    def k_within_trials(self) -> BinomialRequest:
        if self.k > self.n:
            raise ValueError("k must be less than or equal to n")
        return self


CalculationInput = Annotated[
    RatioRequest | BayesRequest | ExpectedValueRequest | NormalCDFRequest | BinomialRequest,
    Field(discriminator="operation"),
]


class CalculationRequest(RootModel[CalculationInput]):
    """A flat JSON object, selected by its ``operation`` field."""


class RatioResult(StrictModel):
    ratio: FiniteFloat


class BayesResult(StrictModel):
    posterior: Probability
    evidence_probability: Probability


class ExpectedValueResult(StrictModel):
    expected_value: FiniteFloat
    variance: Annotated[FiniteFloat, Field(ge=0)]
    standard_deviation: Annotated[FiniteFloat, Field(ge=0)]


class ProbabilityResult(StrictModel):
    probability: Probability


class CalculationResponse(StrictModel):
    operation: Literal["ratio", "bayes", "expected_value", "normal_cdf", "binomial"]
    method: str
    result: RatioResult | BayesResult | ExpectedValueResult | ProbabilityResult
    assumptions: list[str]
    advisory_only: Literal[True] = True


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} cannot be represented as a finite number for these inputs")
    return value


def _binomial_probability(request: BinomialRequest) -> float:
    """Sum mode-centered probability weights, avoiding factorials and cancellation.

    The weights decrease away from the distribution's mode. Normalizing their
    sum cancels the shared constant, avoiding underflow of the initial PMF.
    Tail sums are computed directly, never as ``1 - opposite_tail``.
    """
    n, k, p = request.n, request.k, request.probability
    if p == 0 or p == 1:
        point = 0 if p == 0 else n
        if request.mode == "exact":
            return float(k == point)
        if request.mode == "at_most":
            return float(point <= k)
        return float(point >= k)

    mode = min(n, math.floor((n + 1) * p))
    weights = [0.0] * (n + 1)
    weights[mode] = 1.0
    q = 1.0 - p
    # q / p can overflow for a subnormal p, but then mode is zero and this
    # branch is absent. Compute the ratio only when the branch is needed.
    if mode:
        odds_down = q / p
        for index in range(mode, 0, -1):
            weights[index - 1] = weights[index] * ((index / (n - index + 1)) * odds_down)
    if mode < n:
        odds_up = p / q
        for index in range(mode, n):
            weights[index + 1] = weights[index] * (((n - index) / (index + 1)) * odds_up)

    if request.mode == "exact":
        selected = weights[k]
    elif request.mode == "at_most":
        selected = math.fsum(weights[: k + 1])
    else:
        selected = math.fsum(weights[k:])
    return min(1.0, max(0.0, selected / math.fsum(weights)))


def calculate(request: CalculationRequest) -> CalculationResponse:
    """Calculate a validated request, rejecting undefined or overflowing results.

    Floating-point precision applies; extremely small tail probabilities may
    round to zero. Inputs must already have passed ``CalculationRequest``
    validation. No external inference service is used.
    """
    item = request.root
    assumptions = [
        "Inputs are supplied by the caller; no forecast or empirical calibration is inferred.",
        "Outputs are advisory only and grant no workflow authority.",
    ]
    try:
        if isinstance(item, RatioRequest):
            result = RatioResult(ratio=_finite(item.numerator / item.denominator, "ratio"))
            method = "numerator / denominator"
            assumptions.append("Ratio units are numerator units divided by denominator units.")
        elif isinstance(item, BayesRequest):
            positive_log = (
                math.log(item.prior) + math.log(item.sensitivity)
                if item.prior > 0 and item.sensitivity > 0
                else -math.inf
            )
            negative_log = (
                math.log1p(-item.prior) + math.log(item.false_positive_rate)
                if item.prior < 1 and item.false_positive_rate > 0
                else -math.inf
            )
            largest_log = max(positive_log, negative_log)
            if largest_log == -math.inf:
                raise ValueError("positive evidence has zero probability")
            positive_scaled = math.exp(positive_log - largest_log)
            total_scaled = math.fsum((positive_scaled, math.exp(negative_log - largest_log)))
            evidence = min(1.0, math.exp(largest_log + math.log(total_scaled)))
            if evidence == 0:
                raise ValueError("positive evidence has zero or unrepresentably small probability")
            result = BayesResult(
                posterior=positive_scaled / total_scaled,
                evidence_probability=evidence,
            )
            method = "P(H | E) = P(H) P(E | H) / [P(H) P(E | H) + P(not H) P(E | not H)]"
            assumptions.append(
                "prior=P(H), sensitivity=P(E|H), false_positive_rate=P(E|not H); probabilities use [0,1]."
            )
        elif isinstance(item, ExpectedValueRequest):
            total = math.fsum(outcome.probability for outcome in item.outcomes)
            # Normalize the accepted rounding tolerance to preserve a proper
            # probability distribution, including exactly constant outcomes.
            outcomes = [
                (o.value, o.probability / total) for o in item.outcomes if o.probability > 0
            ]
            constant_value = outcomes[0][0]
            is_constant = all(value == constant_value for value, _ in outcomes)
            mean = (
                constant_value
                if is_constant
                else _finite(
                    math.fsum(value * probability for value, probability in outcomes),
                    "expected value",
                )
            )
            scale = max(abs(value) for value, _ in outcomes)
            if is_constant or scale == 0:
                standard_deviation = 0.0
            else:
                # hypot scales internally, so tiny probabilities combined with
                # huge outcomes do not overflow from squaring the outcome first.
                deviations = []
                for value, probability in outcomes:
                    difference = value - mean
                    scaled_difference = (
                        difference / scale
                        if math.isfinite(difference)
                        else value / scale - mean / scale
                    )
                    deviations.append(math.sqrt(probability) * scaled_difference)
                standard_deviation = _finite(math.hypot(*deviations) * scale, "standard deviation")
            variance = _finite(standard_deviation * standard_deviation, "variance")
            result = ExpectedValueResult(
                expected_value=mean, variance=variance, standard_deviation=standard_deviation
            )
            method = "E[X] = sum(p*x); population variance = sum(p*(x-E[X])^2)"
            assumptions.append(
                "Outcomes are mutually exclusive and exhaustive; probabilities sum to 1 within 1e-9 and are normalized."
            )
            assumptions.append(
                "Expected value and standard deviation use input units; variance uses squared input units."
            )
        elif isinstance(item, NormalCDFRequest):
            difference = item.x - item.mean
            z = (
                difference / item.standard_deviation
                if math.isfinite(difference)
                else item.x / item.standard_deviation - item.mean / item.standard_deviation
            )
            result = ProbabilityResult(probability=0.5 * math.erfc(-z / math.sqrt(2.0)))
            method = "P(X <= x) = 0.5 * erfc(-(x-mean)/(standard_deviation*sqrt(2)))"
            assumptions.append(
                "X is assumed normally distributed; x, mean, and standard deviation share units."
            )
        else:
            result = ProbabilityResult(probability=_binomial_probability(item))
            relation = {"exact": "=", "at_most": "<=", "at_least": ">="}[item.mode]
            method = f"P(X {relation} k) for X ~ Binomial(n, probability), using normalized mode-centered weights"
            assumptions.append(
                "Trials are independent, binary, and have the same success probability; bounds include k."
            )
        return CalculationResponse(
            operation=item.operation, method=method, result=result, assumptions=assumptions
        )
    except (OverflowError, ZeroDivisionError) as exc:
        raise ValueError("calculation is outside the finite floating-point range") from exc

"""Reference cases, boundary contracts, and numerical failure behavior."""

import math

import pytest
from pydantic import ValidationError

from tof_prediction_adapter.calculations import CalculationRequest, calculate


def run(payload):
    return calculate(CalculationRequest.model_validate(payload))


def test_ratio_contract_and_serialization():
    request = CalculationRequest.model_validate(
        {"operation": "ratio", "numerator": 3, "denominator": 2}
    )
    assert request.model_dump() == {"operation": "ratio", "numerator": 3.0, "denominator": 2.0}
    response = calculate(request)
    assert response.result.ratio == 1.5
    assert response.advisory_only is True
    assert response.operation == "ratio"
    assert "units" in " ".join(response.assumptions)
    assert '"ratio":1.5' in response.model_dump_json()


@pytest.mark.parametrize("value", [True, False, "1", None, math.inf, -math.inf, math.nan])
def test_non_numeric_or_nonfinite_ratio_inputs_are_rejected(value):
    with pytest.raises(ValidationError):
        CalculationRequest.model_validate(
            {"operation": "ratio", "numerator": value, "denominator": 2}
        )


@pytest.mark.parametrize("denominator", [0, 0.0, -0.0])
def test_zero_denominator_is_rejected_before_execution(denominator):
    with pytest.raises(ValidationError, match="nonzero"):
        CalculationRequest.model_validate(
            {"operation": "ratio", "numerator": 1, "denominator": denominator}
        )


def test_ratio_rejects_overflow_instead_of_serializing_infinity():
    with pytest.raises(ValueError, match="finite"):
        run({"operation": "ratio", "numerator": 1e308, "denominator": 1e-308})


def test_bayes_accounts_for_base_rate_and_uses_false_positive_rate():
    result = run(
        {"operation": "bayes", "prior": 0.01, "sensitivity": 0.99, "false_positive_rate": 0.05}
    ).result
    assert result.evidence_probability == pytest.approx(0.0594)
    assert result.posterior == pytest.approx(1 / 6)


@pytest.mark.parametrize("prior,expected", [(0.0, 0.0), (1.0, 1.0)])
def test_bayes_prior_boundaries(prior, expected):
    assert (
        run(
            {"operation": "bayes", "prior": prior, "sensitivity": 0.8, "false_positive_rate": 0.2}
        ).result.posterior
        == expected
    )


def test_bayes_impossible_evidence_has_no_defined_posterior():
    with pytest.raises(ValueError, match="evidence"):
        run({"operation": "bayes", "prior": 0.5, "sensitivity": 0.0, "false_positive_rate": 0.0})


def test_expected_value_uses_population_variance_and_signed_values():
    result = run(
        {
            "operation": "expected_value",
            "outcomes": [
                {"value": -1, "probability": 0.5},
                {"value": 2, "probability": 0.5},
            ],
        }
    ).result
    assert result.expected_value == 0.5
    assert result.variance == pytest.approx(2.25)
    assert result.standard_deviation == pytest.approx(1.5)


def test_expected_value_accepts_only_small_probability_rounding_error():
    result = run(
        {
            "operation": "expected_value",
            "outcomes": [
                {"value": 10, "probability": 0.5},
                {"value": 10, "probability": 0.5000000001},
            ],
        }
    ).result
    assert result.expected_value == pytest.approx(10)
    assert result.variance == pytest.approx(0, abs=1e-25)
    with pytest.raises(ValidationError, match="sum to 1"):
        CalculationRequest.model_validate(
            {
                "operation": "expected_value",
                "outcomes": [
                    {"value": 10, "probability": 0.4},
                    {"value": 20, "probability": 0.4},
                ],
            }
        )


def test_bayes_subnormal_masses_preserve_posterior_ratio():
    result = run(
        {
            "operation": "bayes",
            "prior": 5e-324,
            "sensitivity": 0.5,
            "false_positive_rate": 5e-324,
        }
    ).result
    assert result.posterior == pytest.approx(1 / 3, rel=1e-12)
    assert result.evidence_probability > 0


def test_bayes_rejects_evidence_that_cannot_be_represented():
    with pytest.raises(ValueError, match="unrepresentably small"):
        run(
            {
                "operation": "bayes",
                "prior": 5e-324,
                "sensitivity": 0.1,
                "false_positive_rate": 0,
            }
        )


def test_zero_probability_outcome_cannot_create_variance_overflow():
    result = run(
        {
            "operation": "expected_value",
            "outcomes": [
                {"value": 5, "probability": 1},
                {"value": 1e308, "probability": 0},
            ],
        }
    ).result
    assert result.expected_value == 5
    assert result.variance == 0


def test_constant_extreme_outcome_has_exactly_zero_variance_despite_probability_rounding():
    probabilities = [
        0.12614394695077769,
        0.12943418162844555,
        0.1446965084998004,
        0.08121974003563619,
        0.12777691470212996,
        0.12774231870845312,
        0.09511276151983113,
        0.0704645624942216,
        0.028682040614774355,
        0.06872702484592993,
    ]
    result = run(
        {
            "operation": "expected_value",
            "outcomes": [
                {"value": 1e308, "probability": probability} for probability in probabilities
            ],
        }
    ).result
    assert result.expected_value == 1e308
    assert result.variance == 0


def test_rare_extreme_outcome_has_finite_variance_without_squaring_it_first():
    result = run(
        {
            "operation": "expected_value",
            "outcomes": [
                {"value": 0, "probability": 1},
                {"value": 1e200, "probability": 1e-200},
            ],
        }
    ).result
    assert result.expected_value == pytest.approx(1)
    assert result.variance == pytest.approx(1e200)


def test_unrepresentable_variance_is_explicitly_rejected():
    with pytest.raises(ValueError, match="finite"):
        run(
            {
                "operation": "expected_value",
                "outcomes": [
                    {"value": -1e308, "probability": 0.5},
                    {"value": 1e308, "probability": 0.5},
                ],
            }
        )


@pytest.mark.parametrize(
    "x,expected", [(0.0, 0.5), (1.959963984540054, 0.975), (-1.959963984540054, 0.025)]
)
def test_standard_normal_reference_quantiles(x, expected):
    assert run({"operation": "normal_cdf", "x": x}).result.probability == pytest.approx(
        expected, abs=1e-15
    )


def test_normal_cdf_handles_overflowing_difference_without_losing_standardization():
    result = run(
        {"operation": "normal_cdf", "x": 1e308, "mean": -1e308, "standard_deviation": 1e308}
    ).result
    assert result.probability == pytest.approx(0.9772498680518208, abs=1e-15)


@pytest.mark.parametrize("x,expected", [(-1e308, 0.0), (1e308, 1.0)])
def test_normal_cdf_saturates_to_valid_probability_for_extreme_z_scores(x, expected):
    assert (
        run({"operation": "normal_cdf", "x": x, "standard_deviation": 1e-308}).result.probability
        == expected
    )


@pytest.mark.parametrize(
    "mode,expected", [("exact", 0.201326592), ("at_most", 0.8791261184), ("at_least", 0.3222004736)]
)
def test_binomial_reference_probabilities_and_inclusive_bounds(mode, expected):
    result = run(
        {"operation": "binomial", "n": 10, "k": 3, "probability": 0.2, "mode": mode}
    ).result
    assert result.probability == pytest.approx(expected, abs=1e-14)


def test_binomial_extreme_tail_is_not_lost_to_subtraction_from_one():
    result = run(
        {"operation": "binomial", "n": 1000, "k": 950, "probability": 0.5, "mode": "at_least"}
    ).result
    # Independent high-precision reference, calculated from the exact sum of
    # integer binomial coefficients divided by 2**1000.
    expected = sum(math.comb(1000, k) for k in range(950, 1001)) / (2**1000)
    assert result.probability > 0
    assert result.probability == pytest.approx(expected, rel=2e-13, abs=0)


def test_binomial_maximum_trial_count_remains_stable_and_symmetric():
    center = run(
        {"operation": "binomial", "n": 10000, "k": 5000, "probability": 0.5}
    ).result.probability
    left = run(
        {"operation": "binomial", "n": 10000, "k": 5000, "probability": 0.5, "mode": "at_most"}
    ).result.probability
    assert center == pytest.approx(0.007978646139382154, rel=1e-13)
    assert left == pytest.approx((1 + center) / 2, abs=1e-15)


@pytest.mark.parametrize("p", [0.0, 1.0, 5e-324, 1 - 2**-53])
def test_binomial_boundary_probabilities_and_subnormal_inputs(p):
    endpoint = 0 if p < 0.5 else 10
    result = run({"operation": "binomial", "n": 10, "k": endpoint, "probability": p}).result
    assert result.probability == pytest.approx(1, abs=2e-15)


@pytest.mark.parametrize("mode", ["exact", "at_most", "at_least"])
def test_binomial_zero_trials_has_one_outcome(mode):
    assert (
        run(
            {"operation": "binomial", "n": 0, "k": 0, "probability": 0.5, "mode": mode}
        ).result.probability
        == 1
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "ratio", "numerator": 1, "denominator": 2, "execute": "arbitrary code"},
        {"operation": "bayes", "prior": 1.01, "sensitivity": 0.5, "false_positive_rate": 0.1},
        {"operation": "bayes", "prior": 0.1, "sensitivity": True, "false_positive_rate": 0.1},
        {"operation": "bayes", "prior": 0.1, "sensitivity": 0.5, "false_positive_rate": math.nan},
        {"operation": "expected_value", "outcomes": []},
        {"operation": "expected_value", "outcomes": [{"value": 1, "probability": 0.001}] * 1001},
        {"operation": "expected_value", "outcomes": [{"value": True, "probability": 1}]},
        {"operation": "normal_cdf", "x": "1"},
        {"operation": "normal_cdf", "x": 1, "standard_deviation": 0},
        {"operation": "normal_cdf", "x": 1, "standard_deviation": -1},
        {"operation": "binomial", "n": True, "k": 1, "probability": 0.5},
        {"operation": "binomial", "n": 2.0, "k": 1, "probability": 0.5},
        {"operation": "binomial", "n": 2, "k": "1", "probability": 0.5},
        {"operation": "binomial", "n": 10001, "k": 1, "probability": 0.5},
        {"operation": "binomial", "n": 10, "k": 11, "probability": 0.5},
        {"operation": "binomial", "n": -1, "k": 0, "probability": 0.5},
        {"operation": "binomial", "n": 10, "k": 1, "probability": -0.1},
        {"operation": "binomial", "n": 10, "k": 1, "probability": 0.5, "mode": "less_than"},
        {"operation": "eval", "expression": "1 + 1"},
    ],
)
def test_operation_contract_rejects_invalid_or_unbounded_inputs(payload):
    with pytest.raises(ValidationError):
        CalculationRequest.model_validate(payload)

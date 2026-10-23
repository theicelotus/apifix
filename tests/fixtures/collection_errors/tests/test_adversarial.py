from unittest import mock

from src.suspense.adversarial import perturbed_rating


@mock.patch("src.suspense.adversarial.rate", return_value=5)
def test_perturbed_rating(rate):
    assert '"rating": 5' in perturbed_rating("The door creaked.")

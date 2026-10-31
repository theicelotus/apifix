from unittest.mock import patch


def test_other():
    with patch("ailabel.lib.other.thing", return_value=2):
        pass

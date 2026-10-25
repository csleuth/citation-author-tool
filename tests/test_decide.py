"""Offline tests for the v3 pen-name check."""
import pytest
from refs.decide import wordlike_given

GIVEN = {"ahmed": 40, "sterling": 6, "ming": 12, "jane": 300}


@pytest.mark.parametrize("name, flagged", [
    ("Caffeinated Clint", True),   # word-like and never a given name in the corpus
    ("Reckless Rita", True),
    ("Ahmed Khan", False),         # ends in -ed, but a common given name
    ("Sterling Brown", False),
    ("Ming Li", False),
    ("Jane Smith", False),
    ("Uzay Bulut", False),         # unseen, but not word-like
])
def test_wordlike_given(name, flagged):
    assert wordlike_given(name, GIVEN) is flagged

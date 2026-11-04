"""A model may not learn that ordinary words are a misheard name (2026-11-04)."""
import pytest
from wispr import dictionary as d

needs_words = pytest.mark.skipif(not d._english(), reason="no system word list")


@needs_words
@pytest.mark.parametrize("wrong,right", [("army", "Zentrix"), ("the line", "Linora"), ("line", "Linora"), ("linear", "Linora"), ("12 hours", "12Hard")])
def test_real_words_are_never_learned_from_a_model(wrong, right):
    assert d._real_words(wrong, right)


@needs_words
@pytest.mark.parametrize("wrong,right", [("12 hard", "12Hard"), ("Superbase", "Supabase")])
def test_spacing_and_non_words_still_learn(wrong, right):
    assert not d._real_words(wrong, right)

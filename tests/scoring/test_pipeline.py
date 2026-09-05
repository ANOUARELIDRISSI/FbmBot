from __future__ import annotations

from becarscout.analyzer.models import DescriptionSignals
from becarscout.scoring.pipeline import (
    _canonicalize_make,
    _model_query_from_hint,
    resolve_make_and_model_query,
)
from becarscout.structurer.models import StructuredListing


def _signals(**overrides) -> DescriptionSignals:
    defaults = dict(listing_id="l1")
    defaults.update(overrides)
    return DescriptionSignals(**defaults)


def _structured(**overrides) -> StructuredListing:
    defaults = dict(listing_id="l1", url="https://example.test/1", raw_title="some car")
    defaults.update(overrides)
    return StructuredListing(**defaults)


# --- _canonicalize_make ---


def test_canonicalize_make_exact_case_insensitive_match():
    assert _canonicalize_make("volkswagen") == "Volkswagen"
    assert _canonicalize_make("BMW") == "BMW"


def test_canonicalize_make_known_alias():
    assert _canonicalize_make("vw") == "Volkswagen"
    assert _canonicalize_make("VW") == "Volkswagen"


def test_canonicalize_make_unknown_returns_none_not_a_guess():
    # Conservative on purpose -- a wrong canonicalization would silently
    # corrupt the 2dehands cache key for that make.
    assert _canonicalize_make("Wartburg") is None
    assert _canonicalize_make(None) is None


# --- _model_query_from_hint ---


def test_model_query_prefers_known_multi_word_model():
    assert _model_query_from_hint("Grand Cherokee 3.0 CRD 2013 automatic") == "Grand Cherokee"
    assert _model_query_from_hint("C-Class 220d break") == "C-Class"


def test_model_query_falls_back_to_first_word():
    assert _model_query_from_hint("Golf 2.0 TDI 2015") == "Golf"


def test_model_query_handles_empty_input():
    assert _model_query_from_hint(None) is None
    assert _model_query_from_hint("") is None


# --- resolve_make_and_model_query ---


def test_prefers_stage_2_detection_when_available():
    structured = _structured(make="Volkswagen", model_hint="Golf 2.0 TDI")
    signals = _signals(make="Audi", model="A3")  # should be ignored
    make, model = resolve_make_and_model_query(structured, signals)
    assert (make, model) == ("Volkswagen", "Golf")


def test_falls_back_to_llm_make_when_stage_2_found_none():
    structured = _structured(make=None, model_hint=None)
    signals = _signals(make="vw", model="Grand Cherokee")
    make, model = resolve_make_and_model_query(structured, signals)
    assert make == "Volkswagen"
    assert model == "Grand Cherokee"


def test_falls_back_to_llm_model_when_hint_has_no_usable_token():
    structured = _structured(make="Jeep", model_hint=None)
    signals = _signals(model="Grand Cherokee")
    make, model = resolve_make_and_model_query(structured, signals)
    assert (make, model) == ("Jeep", "Grand Cherokee")


def test_no_signals_and_no_stage_2_detection_returns_nones():
    structured = _structured(make=None, model_hint=None)
    make, model = resolve_make_and_model_query(structured, None)
    assert (make, model) == (None, None)


def test_unrecognized_llm_make_does_not_produce_a_fake_fallback():
    structured = _structured(make=None, model_hint=None)
    signals = _signals(make="MadeUpBrand", model="Foo")
    make, model = resolve_make_and_model_query(structured, signals)
    assert make is None

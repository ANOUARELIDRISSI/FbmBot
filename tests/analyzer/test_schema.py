"""Guards against `schema.py` drifting out of sync with `models.py` --
they're kept in sync by hand (see schema.py's module docstring: Mistral's
strict JSON schema mode needs a hand-written dict, not
`DescriptionSignalFields.model_json_schema()`), so nothing else catches a
forgotten field. This exact drift almost happened when `is_whole_vehicle`
was added to fix a real bug (a parts listing scored as a car deal)."""

from __future__ import annotations

from becarscout.analyzer.models import DescriptionSignalFields
from becarscout.analyzer.schema import SIGNALS_JSON_SCHEMA


def test_every_model_field_is_in_the_schema_properties_and_required():
    model_fields = set(DescriptionSignalFields.model_fields.keys())
    schema_properties = set(SIGNALS_JSON_SCHEMA["properties"].keys())
    schema_required = set(SIGNALS_JSON_SCHEMA["required"])

    assert model_fields == schema_properties, (
        f"model/schema properties out of sync: "
        f"in model only={model_fields - schema_properties}, "
        f"in schema only={schema_properties - model_fields}"
    )
    assert model_fields == schema_required, (
        f"model fields not all required in schema (Mistral's strict mode needs every "
        f"field listed, nullable ones included): missing={model_fields - schema_required}"
    )


def test_schema_forbids_additional_properties():
    assert SIGNALS_JSON_SCHEMA["additionalProperties"] is False

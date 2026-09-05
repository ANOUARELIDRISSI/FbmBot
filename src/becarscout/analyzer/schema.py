"""The JSON schema sent to Mistral's strict `json_schema` response format,
and the system prompt describing how to fill it in. Kept as a hand-written
dict rather than generated from `DescriptionSignalFields.model_json_schema()`
because strict mode requires every property to be listed in `required`
(nullable ones included) and `additionalProperties: false` at every
object level — Pydantic's default schema output doesn't shape itself that
way. Keep this in sync with `models.py` if fields change.
"""

from __future__ import annotations

SIGNALS_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "language": {"type": ["string", "null"], "enum": ["nl", "fr", "en", "mixed", "other", None]},
        "warning_light": {"type": "boolean"},
        "warning_light_severity": {"type": ["string", "null"], "enum": ["minor", "major", "unknown", None]},
        "needs_diagnostic": {"type": "boolean"},
        "gearbox_issue": {"type": "boolean"},
        "gearbox_issue_severity": {"type": ["string", "null"], "enum": ["minor", "likely_major", "unknown", None]},
        "engine_issue": {"type": "boolean"},
        "engine_issue_severity": {"type": ["string", "null"], "enum": ["minor", "likely_major", "unknown", None]},
        "accident_damage": {"type": "boolean"},
        "timing_belt_replaced": {"type": "boolean"},
        "inspection_valid": {"type": ["boolean", "null"]},
        "service_history": {"type": "string", "enum": ["complete", "partial", "none", "unknown"]},
        "vat_scheme": {"type": "string", "enum": ["normal", "margin", "unknown"]},
        "for_export": {"type": "boolean"},
        "fuel_type": {"type": ["string", "null"], "enum": ["diesel", "petrol", "hybrid", "electric", "lpg", None]},
        "transmission": {"type": ["string", "null"], "enum": ["manual", "automatic", None]},
        "mileage_km": {"type": ["integer", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": [
        "language", "warning_light", "warning_light_severity", "needs_diagnostic",
        "gearbox_issue", "gearbox_issue_severity", "engine_issue", "engine_issue_severity",
        "accident_damage", "timing_belt_replaced", "inspection_valid", "service_history",
        "vat_scheme", "for_export", "fuel_type", "transmission", "mileage_km", "confidence",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You extract structured facts from Belgian used-car Facebook Marketplace listings. \
Descriptions are informal and may be in Dutch, French, or a mix of both, with \
abbreviations. Extract ONLY what is stated or clearly implied by the text — never \
guess, and never judge whether the car is a good deal; that is not your job.

Field guide:
- warning_light / warning_light_severity / needs_diagnostic: e.g. "motor lampje \
soms aan" (dashboard warning light sometimes on)
- gearbox_issue / gearbox_issue_severity: e.g. "versnellingsbak heeft aandacht \
nodig" (gearbox needs attention) -> likely_major. Note: a parts listing saying \
the gearbox was already removed for sale is NOT a gearbox issue.
- engine_issue / engine_issue_severity: any stated engine problem
- accident_damage: any stated accident history or body damage
- timing_belt_replaced: e.g. "nieuwe distributieriem" (new timing belt) -> true
- inspection_valid: Belgian roadworthiness inspection (keuring / contrôle \
technique) — true if explicitly valid/present, false if explicitly absent or \
needed, null if not mentioned
- service_history: "carnet d'entretien complet" -> complete; partial records \
mentioned -> partial; explicitly none -> none; not mentioned -> unknown
- vat_scheme: "geen BTW" / "marge voertuig" / "margevoertuig" -> margin; \
explicit normal invoice/BTW aftrekbaar -> normal; not mentioned -> unknown
- for_export: "voor export" / "pour export" -> true
- fuel_type, transmission, mileage_km: only set if explicitly mentioned in the \
description — used to cross-check separate regex-based extraction, not to \
replace it
- confidence: your confidence in how complete/clear this description was to \
extract from (high/medium/low) — not a judgment of the car

Respond with a single JSON object matching the given schema. No prose, no markdown.\
"""

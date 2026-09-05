"""Stage [3]: LLM Description Analyzer. Feeds each listing's free-text
description through Mistral with a strict JSON schema so it returns
structured signals, not prose or an opinion (see `schema.py` for the
extraction rules, `models.py` for the output shape).

Only this module talks to the LLM API — everything upstream (scraper,
structurer) and downstream (scoring engine, stage 4+) stays deterministic,
per Project.md's design principle.
"""

from __future__ import annotations

import json
import logging
import os
import time

from dotenv import load_dotenv
from mistralai.client import Mistral
from mistralai.client.errors.sdkerror import SDKError

from becarscout.structurer.models import StructuredListing

from .models import DescriptionSignalFields, DescriptionSignals
from .schema import SIGNALS_JSON_SCHEMA, SYSTEM_PROMPT

logger = logging.getLogger(__name__)

load_dotenv()

# ministral-8b-latest, not mistral-small-latest: the latter returned a hard
# 429 on this account's tier even after long backoffs, while ministral-8b
# worked immediately — looks like a paid-tier gate, not per-request
# throttling. Override via MISTRAL_MODEL once/if the account is upgraded.
DEFAULT_MODEL = os.getenv("MISTRAL_MODEL", "ministral-8b-latest")

# Backoff schedule for actual rate-limit (429) responses only — other
# errors (bad schema, malformed JSON, auth) fail fast instead of retrying.
_RETRY_DELAYS_S = (5, 20, 45)


def _get_client() -> Mistral:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY not set — add it to your .env file.")
    return Mistral(api_key=api_key)


def _is_rate_limited(exc: Exception) -> bool:
    return isinstance(exc, SDKError) and exc.raw_response.status_code == 429


def analyze_description(
    client: Mistral, listing: StructuredListing, *, model: str = DEFAULT_MODEL
) -> DescriptionSignals:
    if not listing.raw_description:
        return DescriptionSignals(listing_id=listing.listing_id, confidence="low")

    user_prompt = f"Title: {listing.raw_title}\n\nDescription:\n{listing.raw_description}"

    last_error: Exception | None = None
    for attempt, delay in enumerate((0, *_RETRY_DELAYS_S)):
        if delay:
            time.sleep(delay)
        try:
            response = client.chat.complete(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "car_listing_signals",
                        "schema": SIGNALS_JSON_SCHEMA,
                        "strict": True,
                    },
                },
            )
            content = response.choices[0].message.content
            fields = DescriptionSignalFields.model_validate(json.loads(content))
            return DescriptionSignals(listing_id=listing.listing_id, **fields.model_dump())
        except Exception as exc:  # network, rate limit, malformed JSON, schema mismatch
            last_error = exc
            logger.warning("Analysis attempt %d failed for %s: %s", attempt + 1, listing.listing_id, exc)
            if not _is_rate_limited(exc):
                break

    logger.error("Giving up on %s (%s): %s", listing.listing_id, listing.url, last_error)
    return DescriptionSignals(listing_id=listing.listing_id, extraction_failed=True, confidence="low")


def analyze_descriptions(
    listings: list[StructuredListing], *, model: str = DEFAULT_MODEL, delay_s: float = 1.0
) -> list[DescriptionSignals]:
    """Sequential by design — small delay between calls to stay clear of
    per-minute rate limits, since these run unattended and correctness
    matters more than throughput here."""
    client = _get_client()
    results = []
    for i, listing in enumerate(listings):
        results.append(analyze_description(client, listing, model=model))
        if delay_s and i < len(listings) - 1:
            time.sleep(delay_s)
    return results

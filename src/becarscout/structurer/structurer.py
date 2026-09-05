"""Stage [2]: turns a stage-1 `RawListing` into a `StructuredListing` by
running the deterministic parsers in `parse.py` over its title,
description, and location text. No interpretation of free text beyond
what a regex/keyword can justify — that's stage 3's job.
"""

from __future__ import annotations

from becarscout.scraper.models import RawListing

from .models import StructuredListing
from .parse import (
    parse_fuel_type,
    parse_listing_age_days,
    parse_location,
    parse_make_and_model_hint,
    parse_mileage_km,
    parse_price,
    parse_transmission,
    parse_year,
    sanity_check_mileage,
)


def structure_listing(raw: RawListing) -> StructuredListing:
    price_eur, is_free = parse_price(raw.price_text)
    make, model_hint = parse_make_and_model_hint(raw.title, raw.description)
    city, region = parse_location(raw.location_text)
    year = parse_year(raw.title, raw.description)
    mileage_km = sanity_check_mileage(
        parse_mileage_km(raw.title, raw.description), year, raw.title, raw.description
    )

    return StructuredListing(
        listing_id=raw.listing_id,
        url=raw.url,
        price_eur=price_eur,
        is_free=is_free,
        make=make,
        model_hint=model_hint,
        year=year,
        mileage_km=mileage_km,
        fuel_type=parse_fuel_type(raw.title, raw.description),
        transmission=parse_transmission(raw.title, raw.description),
        location_city=city,
        location_region=region,
        listing_age_days=parse_listing_age_days(raw.listed_relative_text),
        photo_count=len(raw.photo_urls),
        raw_title=raw.title,
        raw_description=raw.description,
    )


def structure_listings(raws: list[RawListing]) -> list[StructuredListing]:
    return [structure_listing(raw) for raw in raws]

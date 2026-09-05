"""Car makes recognized during structuring, curated for what actually shows
up in the Belgian used-car market (mainstream European brands first, plus
the Japanese/Korean/American brands common there too). This is deliberately
a flat name list, not a make→model catalogue — model detection from free
text is unreliable enough that stage 2 only extracts a "model hint" (see
`parse.py`), not a validated model.

Ordered longest-first so a multi-word make (e.g. "Land Rover") matches
before a shorter one that could be a substring of it, and checked with
word boundaries so short names ("Kia", "Mini") don't match inside unrelated
words.
"""

from __future__ import annotations

_MAKES_RAW = [
    "Volkswagen", "Renault", "Peugeot", "Citroën", "Citroen", "Opel", "Ford",
    "BMW", "Mercedes-Benz", "Mercedes", "Audi", "Volvo", "Škoda", "Skoda",
    "Seat", "Fiat", "Toyota", "Nissan", "Hyundai", "Kia", "Mazda", "Honda",
    "Mitsubishi", "Suzuki", "Dacia", "Mini", "Land Rover", "Range Rover",
    "Jaguar", "Porsche", "Alfa Romeo", "Lexus", "Subaru", "Smart", "Jeep",
    "Chevrolet", "Chrysler", "DS", "Tesla", "Saab", "Lancia", "Iveco",
    "Cadillac", "Dodge", "Buick", "GMC", "Ram", "Infiniti", "Acura",
    "Lincoln", "Ferrari", "Lamborghini", "Bentley", "Rolls-Royce",
    "Aston Martin", "Maserati", "Bugatti",
]

# Longest names first so e.g. "Land Rover" is tried before "Rover" would be.
MAKES: list[str] = sorted(set(_MAKES_RAW), key=len, reverse=True)

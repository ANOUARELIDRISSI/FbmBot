"""Regression tests for stage [2]'s mileage extraction — see Project.md's
Structurer notes. Three real fixtures below reproduce a live bug: mileage
was extracted as a small implausible number (300-500km) grabbed from
somewhere other than the actual odometer reading.
"""

from __future__ import annotations

from becarscout.structurer.parse import parse_mileage_km, sanity_check_mileage
from becarscout.structurer.structurer import structure_listing
from becarscout.scraper.models import RawListing

# --- Real raw text captured from three listings that mis-extracted mileage
# (queried from the SQLite DB by URL; see Project.md / conversation history
# for the original bug report). Kept verbatim (including Facebook's own
# French-locale UI boilerplate) since that boilerplate is exactly what
# caused the bug.

FORD_F150_TITLE = "1986 Ford f-150"
FORD_F150_DESCRIPTION = (
    "Se connecter\nSe connecter\nInformations de compte oubliées ?\nMarketplace\n"
    "Tout parcourir\nVotre compte\nAjouter un article\nLieu\nRedwood City\n·\n"
    "Dans un rayon de 64 km\nCatégories\nVéhicules\nLocation immobilière\n"
    "Appareils électroniques\nArticles de rénovation intérieure\nArticles de sport\n"
    "Articles gratuits\nArticles pour la maison\nDivertissement\nFamille\n"
    "Fournitures de bureau\nHabillement\nInstruments de musique\nJardin et extérieur\n"
    "Jeux et jouets\nLoisirs\nPetites annonces\nProduits pour animaux\n"
    "Ventes immobilières\nPlus de catégories\nVilles à proximité\n"
    "San Carlos (Californie)\nAtherton (Californie)\nMenlo Park\n"
    "West Menlo Park, California\nBelmont (Californie)\nVoir plus\n0:02\n/\n0:09\n"
    "1986 Ford f-150\n8 000 $US\nPublié\nil y a une semaine\ndans\nRedwood City, CA\n"
    "Envoyer un message\nEnvoyer un message\nEnregistrer\nPartager\nEnregistrer\n"
    "Partager\nDescription fournie par le ou la vendeur(se)\n"
    "Vendo cosas para troca asientos y otras cosillas\nRedwood City, CA\n"
    "· La localisation est approximative\nEnvoyer un message\nSélection du jour\n"
    "Redwood City\n·\n64 km\n500 $US\nPiano\nCastro Valley, CA\nGRATUIT\n"
    "Free Gas Grill\nCampbell, CA\n70 $US\nTI-84 Plus Graphing Calculator\n"
    "Richmond, CA\n550 $US\n10k Gold Figaro ID Bracelet\nSanta Clara, CA\n"
    "150 $US\nSereneLife treadmill\nNewark, CA\n1 150 $US\nDesktop\nConcord, CA\n"
    "GRATUIT\nVinyl records\nBerkeley, CA\nGRATUIT\nRoll Top Desk\nSan Jose, CA\n"
    "300 $US\nCuatrimoto\nOakland, CA\nGRATUIT\n"
    "Fellow Stagg EKG Electric Pour-Over Kettle, base not included\nBerkeley, CA\n"
    "250 $US\nSeiko 5 SRPK31\nMountain View, CA\n200 $US\nTanning bed\n"
    "Livermore, CA\n100 $US\nRyobi 1900 PSI 1.2 GPM Electric Pressure Washer\n"
    "San Ramon, CA\n200 $US\nDe Walt table saw\nSanta Clara, CA\nGRATUIT\n"
    "Wheelbarrow\nSan Carlos, CA\n100 $US\n"
    "3.1 Phillip Lim Textured Calfskin Mini Pashli Satchel\nSunnyvale, CA\n"
    "130 $US\nSegway Ninebot E2 Plus Escooter\nSan Jose, CA\n10 $US\nSalt light\n"
    "San Jose, CA\nVoir plus sur Facebook\nVoir plus sur Facebook\n"
    "Adresse e-mail ou numéro de tél.\nMot de passe\nSe connecter\n"
    "Mot de passe oublié ?\nou\nCréer un nouveau compte"
)

DODGE_NITRO_TITLE = "2007 Dodge nitro"
DODGE_NITRO_DESCRIPTION = (
    "Se connecter\nSe connecter\nInformations de compte oubliées ?\nMarketplace\n"
    "Tout parcourir\nVotre compte\nAjouter un article\nLieu\nRichmond\n·\n"
    "Dans un rayon de 64 km\nCatégories\nVéhicules\nLocation immobilière\n"
    "Appareils électroniques\nArticles de rénovation intérieure\nArticles de sport\n"
    "Articles gratuits\nArticles pour la maison\nDivertissement\nFamille\n"
    "Fournitures de bureau\nHabillement\nInstruments de musique\nJardin et extérieur\n"
    "Jeux et jouets\nLoisirs\nPetites annonces\nProduits pour animaux\n"
    "Ventes immobilières\nPlus de catégories\nVilles à proximité\n"
    "San Pablo (Californie)\nNorth Richmond, California\nRollingwood, California\n"
    "El Cerrito\nAlbany (Californie)\nVoir plus\n2007 Dodge nitro\n1 200 $US\n"
    "Publié\nil y a un jour\ndans\nRichmond, CA\nEnvoyer un message\n"
    "Envoyer un message\nEnregistrer\nPartager\nEnregistrer\nPartager\n"
    "Description fournie par le ou la vendeur(se)\nRichmond, CA\n"
    "· La localisation est approximative\nEnvoyer un message\nSélection du jour\n"
    "Richmond\n·\n64 km\n500 $US\nPiano\nCastro Valley, CA\nGRATUIT\n"
    "Vinyl records\nBerkeley, CA\n70 $US\nTI-84 Plus Graphing Calculator\n"
    "Richmond, CA\n150 $US\nSereneLife treadmill\nNewark, CA\n1 150 $US\n"
    "Desktop\nConcord, CA\n30 $US\nMulti purpose stand\nAlameda, CA\n300 $US\n"
    "Cuatrimoto\nOakland, CA\nGRATUIT\n"
    "Fellow Stagg EKG Electric Pour-Over Kettle, base not included\nBerkeley, CA\n"
    "GRATUIT\nFree cement blocks\nVacaville, CA\n140 $US\n"
    "1989 Upper Deck Ken Griffey Jr. Star Rookie #1 PSA 7\nBrentwood, CA\n40 $US\n"
    "Nike San Francisco 49ers Kyle Juszczyk #44 Jersey\nFairfield, CA\n240 $US\n"
    'Milwaukee M12 FUEL 6" Random Orbital Sander Kit (3/16")\nPleasant Hill, CA\n'
    "10 $US\nCat Litter(slightly used)\nSan Francisco, CA\n60 $US\nAirpod Pro 2\n"
    "Vacaville, CA\nGRATUIT\nWheelbarrow\nSan Carlos, CA\nGRATUIT\nMoving Boxes\n"
    "American Canyon, CA\nGRATUIT\nFREE Echelon rower to a good home — Palo Alto, CA.\n"
    "Palo Alto, CA\n111 $US\nPentax Asahi K1000 50mm\nOakland, CA\n"
    "Voir plus sur Facebook\nVoir plus sur Facebook\nAdresse e-mail ou numéro de tél.\n"
    "Mot de passe\nSe connecter\nMot de passe oublié ?\nou\nCréer un nouveau compte"
)

BERLINGOT_TITLE = "Citroën berlingot"
BERLINGOT_DESCRIPTION = (
    "Envoyer un message\nEnvoyer un message\nEnregistrer\nPartager\nEnregistrer\n"
    "Partager\nDescription fournie par le ou la vendeur(se)\nÉtat\n"
    "D’occasion - bon état\nBien lire Bonjours alors je vent Citroën berlingo "
    "5 place dans un état correct zvec 209mille km  moteur 1400cc  quant j’ai "
    "acheter ce véhicule  après 300km la distribution a casser   Au final moteur "
    "couler. Moteur introuvable  j’ai décidée de remettre un 1100 dedans "
    "demare au quart de tour et roule  du coups fraie que j’ai fait. "
    "Nouvelle distribution embraye câble bielette roulement de moyeux gauche "
    "droite  et collecteur d’échappement. Problème le véhicule n’a pas "
    "de puissance pourquoi après quelque recherche  enfaite calculateur. De "
    "1400cc moteur 1100cc  du coups il n’envoie pas les bonne donnée  donc "
    "à vendre pour export  ou remettre moteur de 1400 dedans ou calculateur  "
    "de 1100  sinon bah très bon véhicule\nVoir plus\n"
    "Beloeil, WAL · La localisation est approximative\nEnvoyer un message"
)


def test_ford_f150_no_longer_extracts_sidebar_price_as_mileage():
    # Previously extracted 500km by bridging "...radius of 64 km" (page
    # boilerplate) across a newline to an unrelated sidebar listing's price.
    assert parse_mileage_km(FORD_F150_TITLE, FORD_F150_DESCRIPTION) is None


def test_dodge_nitro_no_longer_extracts_sidebar_price_as_mileage():
    assert parse_mileage_km(DODGE_NITRO_TITLE, DODGE_NITRO_DESCRIPTION) is None


def test_berlingot_extracts_the_odometer_reading_not_the_repair_anecdote():
    # The description mentions "209mille km" (209,000 km, the actual
    # odometer) before a later, unrelated "300km" ("after 300km the timing
    # belt broke") — the fix must prefer the former.
    assert parse_mileage_km(BERLINGOT_TITLE, BERLINGOT_DESCRIPTION) == 209_000


def test_mille_thousand_suffix_is_parsed():
    assert parse_mileage_km(None, "voiture avec 45mille km au compteur") == 45_000
    assert parse_mileage_km(None, "voiture avec 45 mille km au compteur") == 45_000


def test_sanity_check_discards_implausible_low_mileage_on_an_old_car():
    assert sanity_check_mileage(500, 1986, "an old truck, nothing special") is None
    assert sanity_check_mileage(300, 2007, "runs great, no issues mentioned") is None


def test_sanity_check_keeps_low_mileage_when_text_says_the_car_is_new():
    assert sanity_check_mileage(500, 2020, "brand new, 0 km, never driven") == 500
    assert sanity_check_mileage(300, 2024, "voiture neuve, jamais roulée") == 300
    assert sanity_check_mileage(300, 2024, "nieuwe auto, nog nooit gereden") == 300


def test_sanity_check_keeps_low_mileage_for_a_car_from_this_or_a_future_model_year():
    from datetime import date

    this_year = date.today().year
    assert sanity_check_mileage(500, this_year, "no special claim in the text") == 500


def test_sanity_check_does_not_false_positive_on_city_names_containing_new():
    # "Newark, CA" appears in the real Ford F-150 fixture above — must not
    # be mistaken for a "brand new" claim.
    assert sanity_check_mileage(500, 1986, FORD_F150_DESCRIPTION) is None


def test_sanity_check_passes_through_plausible_mileage_untouched():
    assert sanity_check_mileage(120_000, 2013, "some description") == 120_000
    assert sanity_check_mileage(None, 2013, "some description") is None


def test_structure_listing_end_to_end_on_the_real_ford_f150_fixture():
    raw = RawListing(
        listing_id="1501920515033689",
        url="https://www.facebook.com/marketplace/item/1501920515033689/",
        title=FORD_F150_TITLE,
        price_text="8 000 $US",
        description=FORD_F150_DESCRIPTION,
    )
    structured = structure_listing(raw)
    assert structured.mileage_km is None


def test_structure_listing_end_to_end_on_the_real_berlingot_fixture():
    raw = RawListing(
        listing_id="2448622638959650",
        url="https://www.facebook.com/marketplace/item/2448622638959650/",
        title=BERLINGOT_TITLE,
        price_text="1500",
        description=BERLINGOT_DESCRIPTION,
    )
    structured = structure_listing(raw)
    assert structured.mileage_km == 209_000

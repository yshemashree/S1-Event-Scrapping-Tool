import pytest

from s1scraper.classify import classify_activity, classify_tier, schema_type_category


@pytest.mark.parametrize("lo, hi, free, tier", [
    (None, None, False, "TBC"),
    (0.0, None, True, "Standard"),
    (0.0, 1000.0, True, "Standard"),     # free entry is the entry price
    (499, None, False, "Standard"),
    (500, None, False, "Premium"),
    (1500, 4000, False, "Premium"),
    (1549, None, False, "Premium"),      # the Guide's ₹1,501-1,549 gap counts as Premium
    (1550, None, False, "Luxury"),
    (4999, 9999, False, "Luxury"),
    (5000, None, False, "Ultra Premium"),
    (5251, 15000, False, "Ultra Premium"),
])
def test_tier_thresholds_follow_the_guide(lo, hi, free, tier):
    assert classify_tier(lo, hi, free) == tier


def test_tier_basis_options():
    assert classify_tier(999, 15000, False, "max") == "Ultra Premium"
    assert classify_tier(999, 3000, False, "avg") == "Luxury"
    assert classify_tier(999, None, False, "max") == "Premium"


@pytest.mark.parametrize("categories, title, expected", [
    (["Comedy Shows"], "ALLOW ME – Rahul Dua Live", "Comedy"),
    (["Music Shows", "Concerts"], "Dream Theater – 40th Anniversary Tour India", "Music"),
    (["Sports"], "India vs Brazil – International Friendly", "Sports to Watch"),
    (["Sports"], "Tata Mumbai Marathon 2027", "Recreational Sports"),
    (["Workshops", "Arts & Crafts"], "Pottery Workshop", "Workshops"),
    (["Plays"], "Aadhe Adhure", "Theatre"),
    (["Food & Drinks"], "Sunday Gourmet Brunch", "F&B"),
    (["Food & Drinks"], "Delhi Wine & Cheese Soirée", "F&B"),
    (["Nightlife"], "Techno Tuesdays", "Music"),
    (["Exhibitions"], "Seema Kohli Solo Show", "Art"),
    (["Performances"], "Kathak by Kumudini", "Culture"),
    (["Kids"], "Peppa Pig Live", "Other"),
    (["Spirituality"], "Satsang", "Culture"),
    (["Talks"], "TEDx Mumbai", "Culture"),
])
def test_platform_categories_decide_first(categories, title, expected):
    assert classify_activity(categories, title) == expected


# (title, notes, StepOne's own label) - rows from the client's hand-built Master
REFERENCE_ROWS = [
    ("Amaal Mallik Live at Quake Arena", "Bollywood composer-singer live concert.", "Music"),
    ("Women's T20 World Cup 2026", "India hosts the Women's T20 WC.", "Sports to Watch"),
    ("Aadhe Adhure – National Theatre", "Mohan Rakesh's landmark Hindi play.", "Theatre"),
    ("Exhibit 320 – Sareena Khemka & Kaushik Saha", "Dual solo shows. Contemporary Indian art.", "Art"),
    ("Korean Food Festival – Tipsy Tiger Mumbai", "Korean street food, BBQ, desserts.", "F&B"),
    ("NAAR x Michelin Luxury Dining Series", "Chef Prateek Sadhu collaborations.", "F&B"),
    ("Tour of Thekkady Cycling Gran Fondo 2026", "Premier Gran Fondo cycling event.", "Recreational Sports"),
    ("Iron Maiden – Run For Your Lives Tour Australia", "Eighth tour to Australia.", "Music"),
    ("Hemis Festival 2026 – Ladakh", "Rare Buddhist masked dance festival.", "Culture"),
    ("NRL State of Origin – Game III", "NSW vs Queensland.", "Sports to Watch"),
    ("Love, Death & Ketchup – Varun Grover", "Award-winning writer-comedian's new special.", "Comedy"),
    ("Mana Run Hyderabad 2026", "Popular city run event.", "Recreational Sports"),
    ("BWF World Championships 2026", "Badminton's most prestigious tournament.", "Sports to Watch"),
    ("Formula 1 Singapore Grand Prix 2026", "Iconic night race.", "Sports to Watch"),
    ("Onam Celebrations – Kerala", "Snake boat races, floral carpets, cultural performances.", "Culture"),
    ("IAF Editions – Ahmedabad", "India Art Fair's satellite edition.", "Art"),
    ("Riverdance – 30th Anniversary Tour", "Irish dance spectacular.", "Music"),
    ("Women's Indian Open Golf 2026", "Women's golf tour event on home turf.", "Recreational Sports"),
    ("Art Mumbai 2026 (ART MUMBAI)", "Flagship contemporary art fair.", "Art"),
    ("Pushkar Camel Fair 2026", "Camel trading, folk music, sports races.", "Culture"),
    ("Prithvi Theatre Festival", "Best of Indian theatre.", "Theatre"),
    ("Anyma – ÆDEN World Tour", "Immersive cinematic techno show.", "Music"),
    ("Sunburn Festival 2026", "Asia's largest EDM festival.", "Music"),
    ("Rimsky-Korsakov's Scheherazade", "Live orchestral performance.", "Music"),
]


def test_agrees_with_stepone_labels_from_titles_alone():
    hits = sum(classify_activity([], t, n) == label for t, n, label in REFERENCE_ROWS)
    assert hits / len(REFERENCE_ROWS) >= 0.9, hits


def test_martial_arts_is_not_art_and_carnival_tour_is_music():
    assert classify_activity([], "Asian Indoor & Martial Arts Games – Riyadh 2026") == "Sports to Watch"
    assert classify_activity([], "Jay Chou – Carnival II World Tour Sydney") == "Music"
    assert classify_activity([], "Novelbright – Singapore Debut", "Japanese rock band. Three-night run.") == "Music"


def test_unknown_is_other_and_schema_types_map():
    assert classify_activity([], "Untitled") == "Other"
    assert schema_type_category(["ComedyEvent"]) == "Comedy"
    assert schema_type_category(["https://schema.org/TheaterEvent"]) == "Theatre"
    assert schema_type_category(["Event"]) is None


# Rows from a real Mumbai run that the platforms' own categories got wrong
@pytest.mark.parametrize("categories, title, venue, expected", [
    (["Music"], "Raas Rang Ghatkopar", "Police Hockey Ground, Ghatkopar East", "Culture"),
    (["Music"], "Showglitz Events Navratri 2026 Ft. Geeta Rabari", "", "Culture"),
    (["Culture"], "Duos ft. Prashasti Singh & Atul Khatri: KCC", "Khar Comedy Club: Mumbai", "Comedy"),
    (["shooting"], "The Rising Gun Shooting Range", "The Rising Gun Shooting Academy", "Recreational Sports"),
    (["Culture"], "OktoberFest by Mumbai Meri Jaan", "MMRDA Grounds", "F&B"),
    (["Workshops"], "AZAD CRICKET COACHING CENTRE MUMBAI OUTDOOR CRICKET TRAINING CAMP 2026", "", "Recreational Sports"),
])
def test_priority_rules_fix_misleading_categories(categories, title, venue, expected):
    assert classify_activity(categories, title, "", venue) == expected

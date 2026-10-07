import pytest

from s1scraper.cities import detect_city, detect_other_city, get_city, is_city_name, resolve_cities


@pytest.mark.parametrize("text, city", [
    ("St. Dominic Road, Bandra West, Mumbai, 400050", "Mumbai"),
    ("Kharghar, Navi Mumbai", "Mumbai"),
    ("Mumbai Masala, Koregaon Park, Pune", "Pune"),            # right-most city wins
    ("Delhi Darbar, Colaba, Mumbai", "Mumbai"),
    ("DLF Cyberhub, Gurugram", "Delhi NCR"),
    ("Sector 18, Noida", "Delhi NCR"),
    ("UB City, Bangalore", "Bengaluru"),
    ("Salt Lake, Kolkata", "Kolkata"),
    ("SG Highway, Ahmedabad", "Ahmedabad"),
    ("TTK Road, Alwarpet, Chennai", "Chennai"),
    ("Koramangala", "Bengaluru"),                              # neighbourhood alone
    ("HITEC City, Hyderabad", None),                           # not one of the seven
    ("", None),
])
def test_detect_city(text, city):
    found = detect_city(text)
    assert (found.name if found else None) == city


def test_lookup_helpers():
    assert get_city("Bangalore").name == "Bengaluru"
    assert get_city("delhi").name == "Delhi NCR"
    assert get_city("Paris") is None
    assert [c.name for c in resolve_cities(["Pune", "pune", "Chennai", "Nowhere"])] == ["Pune", "Chennai"]
    assert is_city_name("Mumbai") and is_city_name("Navi Mumbai 400614") and is_city_name("New Delhi")
    assert not is_city_name("Bandra West") and not is_city_name("Koregaon Park")


def test_other_cities_are_recognised_only_without_one_of_ours():
    assert detect_other_city("Hitec City, Hyderabad, Telangana 500081") == "Hyderabad"
    assert detect_other_city("Hyderabad House, New Delhi") is None
    assert detect_other_city("Bandra West") is None
    assert detect_other_city("") is None

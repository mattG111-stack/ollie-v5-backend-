"""Auckland Unitary Plan zoning rules.

Direct port of the 'subdividon rules' tab in the client's Excel.
Zones with min_lot_m2 = None are not subdividable for residential purposes
(Business, Open Space, Coastal, Special Purpose, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ZoneRule:
    name: str
    min_lot_m2: int | None
    max_dwellings: int | None
    minor_dwelling: bool
    # is_urban_residential = subdivision economics make sense at residential $/m² rates.
    # Rural/large-lot zones can be subdividable in theory, but valuing 80,000 m² lots at
    # residential rates produces absurd $50M+ "section values". For those we still flag
    # `additional_lots` informationally but don't compute total_subdivided_value.
    is_urban_residential: bool = False


ZONE_RULES: dict[str, ZoneRule] = {
    "Residential - Single House Zone":                              ZoneRule("Residential - Single House Zone", 600, 1, True, True),
    "Residential - Mixed Housing Suburban Zone":                    ZoneRule("Residential - Mixed Housing Suburban Zone", 300, 3, True, True),
    "Residential - Mixed Housing Urban Zone":                       ZoneRule("Residential - Mixed Housing Urban Zone", 300, 3, False, True),
    "Residential - Terrace Housing and Apartment Building Zone":    ZoneRule("Residential - Terrace Housing and Apartment Building Zone", 1200, 99, False, True),
    "Residential - Large Lot Zone":                                 ZoneRule("Residential - Large Lot Zone", 4000, 1, True, False),
    "Residential - Rural and Coastal Settlement Zone":              ZoneRule("Residential - Rural and Coastal Settlement Zone", 2500, 1, True, False),
    "Rural - Countryside Living Zone":                              ZoneRule("Rural - Countryside Living Zone", 20000, 1, False, False),
    "Rural - Rural Production Zone":                                ZoneRule("Rural - Rural Production Zone", 80000, 1, False, False),
    "Rural - Mixed Rural Zone":                                     ZoneRule("Rural - Mixed Rural Zone", 80000, 1, False, False),
    "Rural - Rural Coastal Zone":                                   ZoneRule("Rural - Rural Coastal Zone", 80000, 1, False, False),
    "Rural - Rural Conservation Zone":                              ZoneRule("Rural - Rural Conservation Zone", 200000, 1, False, False),
    "Rural - Waitakere Ranges Zone":                                ZoneRule("Rural - Waitakere Ranges Zone", 200000, 1, False, False),
    "Rural - Waitakere Foothills Zone":                             ZoneRule("Rural - Waitakere Foothills Zone", 80000, 1, False, False),
}

NON_RESIDENTIAL_ZONES = frozenset({
    "Future Urban Zone",
    "Hauraki Gulf Islands",
    "Business - City Centre Zone",
    "Business - Metropolitan Centre Zone",
    "Business - Town Centre Zone",
    "Business - Local Centre Zone",
    "Business - Neighbourhood Centre Zone",
    "Business - Mixed Use Zone",
    "Business - General Business Zone",
    "Business - Light Industry Zone",
    "Business - Heavy Industry Zone",
    "Open Space - Conservation Zone",
    "Open Space - Informal Recreation Zone",
    "Open Space - Sport and Active Recreation Zone",
    "Open Space - Community Zone",
    "Coastal - General Coastal Marine Zone",
    "Coastal - Coastal Transition Zone",
    "Coastal - Marina Zone",
    "Special Purpose - Major Recreation Facility Zone",
    "Special Purpose - Māori Purpose Zone",
    "Special Purpose - School Zone",
    "Special Purpose - Healthcare Facility and Hospital Zone",
    "Special Purpose - Cemetery Zone",
    "Strategic Transport Corridor Zone",
    "Road",
    "Green Infrastructure Corridor",
})


# Urban residential zones whose lots are, by definition, small (300–800 m²).
# A very large lot carrying one of these is a scrape mislabel, not a giant urban
# section — see corrected_zoning().
_URBAN_RESIDENTIAL = frozenset({
    "Residential - Mixed Housing Suburban Zone",
    "Residential - Mixed Housing Urban Zone",
    "Residential - Single House Zone",
    "Residential - Terrace Housing and Apartment Building Zone",
})


def corrected_zoning(zoning: str | None, suburb: str | None = None,
                     land_area=None, address: str | None = None) -> str | None:
    """Repair known-mislabelled scrape zonings before they drive subdivision.

    The scrape sometimes tags large semi-rural blocks with an urban residential
    zone — an 11,300 m² "Mixed Housing Suburban" section does not exist. Confirmed
    locally on Kirian Lane, Waiuku, where lifestyle blocks were tagged MHS and
    valued as 19-lot subdivisions. Reclassify those to rural so they are not
    treated as urban subdivision plays.

    Deliberately NARROW (Waiuku, plus Kirian Lane by name) — we have no
    authoritative Unitary Plan layer, and genuine large MHS greenfield blocks in
    real growth cells (Flat Bush, Karaka) must be left alone. Widen only with GIS.
    """
    # Inputs may arrive as pandas NaN floats — coerce defensively.
    z = zoning.strip() if isinstance(zoning, str) else ""
    addr = address.lower() if isinstance(address, str) else ""
    sub = suburb.strip().lower() if isinstance(suburb, str) else ""
    if "kirian lane" in addr:
        return "Rural - Mixed Rural Zone"
    if sub == "waiuku" and z in _URBAN_RESIDENTIAL:
        try:
            if land_area is not None and float(land_area) > 4000:
                return "Rural - Mixed Rural Zone"
        except (TypeError, ValueError):
            pass
    return zoning


def lookup(zone: str | None) -> ZoneRule | None:
    if not zone:
        return None
    return ZONE_RULES.get(str(zone).strip())


def is_residential_subdividable_zone(zone: str | None) -> bool:
    return lookup(zone) is not None


def classify_zone(zone: str | None) -> str:
    """One of: missing | subdividable | excluded | unknown.

    'excluded' and 'unknown' both end up not-subdividable, but they mean very
    different things: 'excluded' is a deliberate call recorded in
    NON_RESIDENTIAL_ZONES, while 'unknown' is a zone string we've never seen —
    a council rename or a new scrape format quietly dropping listings. Without
    this distinction the two are indistinguishable, so the audit can't tell you
    when coverage breaks.
    """
    if not zone or not str(zone).strip():
        return "missing"
    z = str(zone).strip()
    if z in ZONE_RULES:
        return "subdividable"
    if z in NON_RESIDENTIAL_ZONES:
        return "excluded"
    return "unknown"

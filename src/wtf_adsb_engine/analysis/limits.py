"""Rated operating limits: ISA atmosphere, airspeed conversions, and verdicts.

A type certificate data sheet (TCDS) states a type's maximum operating altitude
and its airspeed limit as Vmo (KIAS) and Mmo, or Vne for light aircraft. ADS-B
reports ground speed, so a comparison needs the wind (see wind.py) or the
aircraft's own true-airspeed report, then the ISA atmosphere to turn true
airspeed into equivalent airspeed and Mach at the reported pressure altitude.

The site's definition of "significant": 1,000 ft above the certified maximum
operating altitude, or 10% over the airspeed limit, sustained. A reading 50% or
more over a limit is treated as bad data, never as a performance claim.
Indicated airspeed is compared with equivalent airspeed, which is at or below
calibrated airspeed, so the comparison understates rather than overstates.
"""

import math
from functools import lru_cache
from typing import NamedTuple

from .numeric import number

FT_TO_M = 0.3048
KT_TO_MS = 1852 / 3600
# ISA (ICAO Doc 7488): sea-level conditions, the lapse rate and the layer bases.
SEA_LEVEL_TEMPERATURE_K = 288.15
SEA_LEVEL_PRESSURE_HPA = 1013.25
GRAVITY = 9.80665
GAS_CONSTANT = 287.05287
GAMMA = 1.4
TROPOPAUSE_M = 11000
STRATOPAUSE_M = 20000
LAPSE_K_PER_M = 0.0065
UPPER_LAPSE_K_PER_M = -0.001
TROPOPAUSE_TEMPERATURE_K = SEA_LEVEL_TEMPERATURE_K - LAPSE_K_PER_M * TROPOPAUSE_M
TROPOPAUSE_PRESSURE_HPA = SEA_LEVEL_PRESSURE_HPA * (TROPOPAUSE_TEMPERATURE_K / SEA_LEVEL_TEMPERATURE_K) ** (
    GRAVITY / (LAPSE_K_PER_M * GAS_CONSTANT)
)
STRATOPAUSE_PRESSURE_HPA = TROPOPAUSE_PRESSURE_HPA * math.exp(
    -GRAVITY * (STRATOPAUSE_M - TROPOPAUSE_M) / (GAS_CONSTANT * TROPOPAUSE_TEMPERATURE_K)
)
# Verdict thresholds (the site's "significant" and "bad data" definitions).
EXCEEDED_ALTITUDE_FT = 1000
EXCEEDED_SPEED_RATIO = 1.10
IMPLAUSIBLE_RATIO = 1.5
# A reading over a limit is sustained over at least this many consecutive reports
# spanning this long; rules.MIN_REPORTS and rules.MIN_REPORT_SECONDS are these.
SUSTAINED_REPORTS = 5
SUSTAINED_SECONDS = 60
# Without a certified ceiling, the highest altitude an airspeed limit is searched to.
DEFAULT_CEILING_FT = 45000


class Limit(NamedTuple):
    """How one certified limit is named: in facts, in the certificate, in prose and on the card."""

    field: str
    unit: str
    symbol: str | None
    name: str
    card_key: str
    card_label: str


# Each certified limit by its dataset key (reference.limits). `field` is the short name the
# story facts give it, `unit` the one the certificate states it in, `symbol` its usual
# abbreviation, `name` how prose calls it, and `card_key`/`card_label` its public measurement.
LIMITS = {
    "max_operating_altitude_ft": Limit(
        "max_operating_altitude_ft",
        "ft",
        None,
        "maximum operating altitude",
        "rated_max_altitude_ft",
        "Certified maximum operating altitude",
    ),
    "vmo_kias": Limit(
        "vmo",
        "KIAS",
        "Vmo",
        "maximum operating speed",
        "rated_vmo_kias",
        "Certified maximum operating speed (Vmo)",
    ),
    "vne_kias": Limit(
        "vne",
        "KIAS",
        "Vne",
        "never-exceed speed",
        "rated_vne_kias",
        "Certified never-exceed speed (Vne)",
    ),
    "mmo": Limit(
        "mmo",
        "Mach",
        "Mmo",
        "maximum operating Mach number",
        "rated_mmo",
        "Certified maximum operating Mach number (Mmo)",
    ),
}
# The indicated-airspeed limits, in the order speed_limit() takes them: Vmo, else Vne.
SPEED_FIELDS = tuple(key for key, limit in LIMITS.items() if limit.unit == "KIAS")


def _metres(h_ft):
    h = number(h_ft)
    if h is None:
        raise ValueError("A pressure altitude is required")
    # Below the ISA model's floor the sea-level layer still applies.
    return max(-2000.0, h) * FT_TO_M


def isa_temperature_k(h_ft):
    """ISA temperature at a pressure altitude in feet."""
    h = _metres(h_ft)
    if h <= TROPOPAUSE_M:
        return SEA_LEVEL_TEMPERATURE_K - LAPSE_K_PER_M * h
    if h <= STRATOPAUSE_M:
        return TROPOPAUSE_TEMPERATURE_K
    return TROPOPAUSE_TEMPERATURE_K - UPPER_LAPSE_K_PER_M * (h - STRATOPAUSE_M)


def isa_pressure_hpa(h_ft):
    """ISA static pressure at a pressure altitude in feet."""
    h = _metres(h_ft)
    if h <= TROPOPAUSE_M:
        return SEA_LEVEL_PRESSURE_HPA * (isa_temperature_k(h_ft) / SEA_LEVEL_TEMPERATURE_K) ** (
            GRAVITY / (LAPSE_K_PER_M * GAS_CONSTANT)
        )
    if h <= STRATOPAUSE_M:
        return TROPOPAUSE_PRESSURE_HPA * math.exp(
            -GRAVITY * (h - TROPOPAUSE_M) / (GAS_CONSTANT * TROPOPAUSE_TEMPERATURE_K)
        )
    return STRATOPAUSE_PRESSURE_HPA * (isa_temperature_k(h_ft) / TROPOPAUSE_TEMPERATURE_K) ** (
        GRAVITY / (UPPER_LAPSE_K_PER_M * GAS_CONSTANT)
    )


def density_ratio(h_ft):
    """Air density over sea-level density (sigma) in the ISA."""
    return (isa_pressure_hpa(h_ft) / SEA_LEVEL_PRESSURE_HPA) / (
        isa_temperature_k(h_ft) / SEA_LEVEL_TEMPERATURE_K
    )


def speed_of_sound_kt(h_ft):
    return math.sqrt(GAMMA * GAS_CONSTANT * isa_temperature_k(h_ft)) / KT_TO_MS


def equivalent_airspeed_kt(tas_kt, h_ft):
    """Equivalent airspeed from true airspeed at a pressure altitude."""
    return number(tas_kt) * math.sqrt(density_ratio(h_ft))


def true_airspeed_kt(eas_kt, h_ft):
    """True airspeed from equivalent airspeed at a pressure altitude."""
    return number(eas_kt) / math.sqrt(density_ratio(h_ft))


def mach(tas_kt, h_ft):
    return number(tas_kt) / speed_of_sound_kt(h_ft)


def tas_from_ground_speed(gs_kt, track_deg, wind_speed_kt, wind_direction_deg):
    """True airspeed as the vector difference of ground velocity and wind.

    Wind direction is where the wind comes from (meteorological convention), so
    a wind from 270° at 50 kt moves the air toward 090°. Returns (tas_kt,
    heading_deg) or None when a value is missing.
    """
    gs, track = number(gs_kt), number(track_deg)
    speed, direction = number(wind_speed_kt), number(wind_direction_deg)
    if None in (gs, track, speed, direction) or gs < 0 or speed < 0:
        return None
    ground = (gs * math.sin(math.radians(track)), gs * math.cos(math.radians(track)))
    toward = math.radians(direction + 180)
    air = (speed * math.sin(toward), speed * math.cos(toward))
    east, north = ground[0] - air[0], ground[1] - air[1]
    tas = math.hypot(east, north)
    heading = math.degrees(math.atan2(east, north)) % 360 if tas > 1e-9 else track % 360
    return tas, heading


def source_for(entry, key):
    """The sheet one certified limit (a dataset key such as "vmo_kias") is cited from.

    An entry's ``sources`` (reference.limits) names each limit's own sheet; an
    entry without one for the key cites its ``source``. A copy, so evidence never
    shares the cached entry's dictionaries.
    """
    sources = entry.get("sources")
    found = sources.get(key) if isinstance(sources, dict) else None
    return dict(found if isinstance(found, dict) else entry["source"])


def speed_limit(entry):
    """(dataset key, KIAS limit as the entry states it) of an entry's airspeed limit, or None."""
    for field in SPEED_FIELDS:
        value = (entry or {}).get(field)
        if (number(value) or 0) > 0:
            return field, value
    return None


def binding_limit(entry, h_ft):
    """(limiting true airspeed, limit name) at a pressure altitude: Vmo or Vne as a true
    airspeed, or Mmo, whichever is lower there; None without an airspeed limit."""
    bounds = []
    limit = speed_limit(entry)
    if limit is not None:
        bounds.append((true_airspeed_kt(limit[1], h_ft), limit[0]))
    mmo = number((entry or {}).get("mmo"))
    if mmo is not None and mmo > 0:
        bounds.append((mmo * speed_of_sound_kt(h_ft), "mmo"))
    return min(bounds) if bounds else None


def limiting_tas_kt(entry, h_ft):
    """The true airspeed at which the entry's airspeed limit is reached at this altitude, or None."""
    bound = binding_limit(entry, h_ft)
    return None if bound is None else bound[0]


def ratio_verdict(ratio):
    """ "exceeded" from 10% over a limit, "implausible" from 50% over (bad data), else "within"."""
    if ratio >= IMPLAUSIBLE_RATIO:
        return "implausible"
    return "exceeded" if ratio >= EXCEEDED_SPEED_RATIO else "within"


def speed_verdict(entry, tas_kt, alt_baro_ft):
    """ratio_verdict() of a true airspeed over the limit that binds at its altitude (Vmo or Vne
    as an equivalent airspeed, or Mmo); None without an airspeed limit, a true airspeed or an
    altitude."""
    tas, altitude = number(tas_kt), number(alt_baro_ft)
    if tas is None or altitude is None or tas < 0:
        return None
    limiting = limiting_tas_kt(entry, altitude)
    return None if limiting is None else ratio_verdict(tas / limiting)


def max_plausible_ground_speed_kt(entry, h_ft, wind_allowance_kt):
    """The ground speed above which a sustained reading is bad data, not a record.

    The limiting true airspeed at the altitude, or without one the largest
    limiting true airspeed up to the type's maximum operating altitude, plus
    the wind allowance. None when the entry states no airspeed limit.
    """
    allowance = number(wind_allowance_kt) or 0
    if number(h_ft) is not None:
        tas = limiting_tas_kt(entry, h_ft)
    else:
        tas = _fastest_limiting_tas(*(number((entry or {}).get(key)) for key in LIMITS))
    return None if tas is None else tas + allowance


@lru_cache(maxsize=1024)
def _fastest_limiting_tas(ceiling, vmo, vne, mmo):
    """The largest limiting true airspeed from sea level to the ceiling, in 1,000-ft steps.

    Cached by the figures themselves: a type's answer never changes, and the
    search steps through every level up to its ceiling.
    """
    entry = {"vmo_kias": vmo, "vne_kias": vne, "mmo": mmo}
    candidates = [
        limiting_tas_kt(entry, height) for height in range(0, int(ceiling or DEFAULT_CEILING_FT) + 1000, 1000)
    ]
    candidates = [value for value in candidates if value is not None]
    return max(candidates) if candidates else None


def altitude_verdict(entry, alt_baro_ft):
    """ "exceeded" from 1,000 ft above the maximum operating altitude, "implausible"
    from 1.5 times it (bad data), else "within"; None without a limit or a reading."""
    limit, altitude = number((entry or {}).get("max_operating_altitude_ft")), number(alt_baro_ft)
    if limit is None or limit <= 0 or altitude is None:
        return None
    if altitude >= limit * IMPLAUSIBLE_RATIO:
        return "implausible"
    return "exceeded" if altitude >= limit + EXCEEDED_ALTITUDE_FT else "within"


# How wind.correct_sample obtained a corrected report's airspeed.
AIRSPEED_BASES = ("reported", "modelled_wind")


def _span(stamps):
    return {
        "reports": len(stamps),
        "seconds": round(max(stamps) - min(stamps), 1),
        "first": min(stamps),
        "last": max(stamps),
    }


def altitude_comparison(entry, samples):
    """The reports over the certified ceiling among confirming samples ({"ts", "value"}):
    their count and span, the lowest of them, and its margin. None without one.

    Each figure is a floor over those reports, so "N reports put it at X or more"
    holds for every one of them; reports below the limit are never counted.
    """
    limit = number((entry or {}).get("max_operating_altitude_ft"))
    over = [
        (stamp, value)
        for sample in samples or ()
        if isinstance(sample, dict)
        and (stamp := number(sample.get("ts"))) is not None
        and (value := number(sample.get("value"))) is not None
        and altitude_verdict(entry, value) == "exceeded"
    ]
    if not over or not limit:
        return None
    lowest = min(value for _stamp, value in over)
    return {
        "altitude_ft": lowest,
        "margin_ft": math.floor(lowest - limit),
        "margin_percent": math.floor((lowest - limit) / limit * 1000) / 10,
        **_span([stamp for stamp, _value in over]),
    }


def exceeded_run(samples, reports=SUSTAINED_REPORTS, seconds=SUSTAINED_SECONDS):
    """The longest run of consecutive "exceeded" reports among corrected samples
    (wind.correct_sample's output) with at least ``reports`` reports spanning
    ``seconds``, oldest first; [] without one.

    Reports are taken in time order, and any other verdict between two exceeded
    reports ends a run: a reading over the limit counts as sustained only while
    nothing in between disagrees. Longest by span, then by count; the earliest
    run on a tie. Only runs that qualify compete, so a long sparse run never
    displaces a sustained one.
    """
    ordered = sorted(
        (s for s in samples or () if isinstance(s, dict) and number(s.get("ts")) is not None),
        key=lambda sample: number(sample["ts"]),
    )
    runs, run = [], []
    for sample in ordered:
        if sample.get("verdict") == "exceeded":
            run.append(sample)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)

    def span(each):
        return number(each[-1]["ts"]) - number(each[0]["ts"])

    runs = [each for each in runs if len(each) >= reports and span(each) >= seconds]
    return max(runs, key=lambda each: (span(each), len(each))) if runs else []


def speed_comparison(entry, samples):
    """The corrected reports over the airspeed limit (wind.correct_sample's output) in their
    longest sustained run (exceeded_run): their count and span, the lowest ground speed
    among them, and the decisive report.

    The decisive report is the one closest to the limit: the smallest ratio of its
    airspeed to the limit that binds at its own altitude. Its ground speed, airspeed,
    Mach, equivalent airspeed (when Vmo or Vne binds; Mmo is compared by Mach), wind,
    and basis all come from that one report, so a story's "this ground speed works
    out to that airspeed" is one calculation, and its margin is a floor over every
    report counted. Reports over the limit outside that run are not counted, as the
    verdict (wind.resolve_rated_speed) does not count them. None without a sustained
    run (a window settled under the earlier, non-consecutive rule may have none, and
    curation then refuses it as rated_incomplete), or when the decisive report's
    airspeed origin is unknown or a modelled airspeed kept no wind.
    """
    over = []
    for sample in exceeded_run(samples):
        stamp, tas, altitude, gs = (
            number(sample.get(key)) for key in ("ts", "airspeed_kt", "alt_baro", "gs")
        )
        bound = binding_limit(entry, altitude) if altitude is not None else None
        if None in (stamp, tas, gs) or bound is None:
            continue
        over.append((tas / bound[0], stamp, sample, bound))
    if not over:
        return None
    ratio, _stamp, decisive, (_limiting, name) = min(over, key=lambda item: item[:2])
    basis = decisive.get("airspeed_basis")
    wind = decisive.get("wind") if isinstance(decisive.get("wind"), dict) else None
    if basis not in AIRSPEED_BASES or (basis == "modelled_wind" and wind is None):
        return None
    altitude, tas = number(decisive["alt_baro"]), number(decisive["airspeed_kt"])
    summary = {
        "ground_speed_kt": min(number(item[2]["gs"]) for item in over),
        "decisive_ground_speed_kt": number(decisive["gs"]),
        "airspeed_kt": tas,
        "airspeed_basis": basis,
        "mach": round(mach(tas, altitude), 3),
        "altitude_ft": altitude,
        "binding_limit": name,
        # Floors, to a tenth: "at least 10.9 percent" never rounds a smaller margin up.
        "margin_percent": math.floor((ratio - 1) * 1000) / 10,
        "wind": wind if basis == "modelled_wind" else None,
        **_span([item[1] for item in over]),
    }
    if name != "mmo":
        # Vmo and Vne are stated as indicated airspeeds, which an equivalent airspeed approximates.
        summary["equivalent_airspeed_kt"] = round(equivalent_airspeed_kt(tas, altitude), 1)
    return summary


def comparison(kind, evidence):
    """The comparison a rated finding's story states, from its saved evidence, or None.

    above_rated_altitude reads the collector's confirming reports, above_rated_speed
    the reports wind.py corrected into its rated_limit block. Curation refuses a
    finding without one (rated_incomplete), and the evidence packet carries it.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    block = evidence.get("rated_limit") if isinstance(evidence.get("rated_limit"), dict) else {}
    if kind == "above_rated_altitude":
        confirmation = evidence.get("confirmation") if isinstance(evidence.get("confirmation"), dict) else {}
        return altitude_comparison(
            {"max_operating_altitude_ft": block.get("limit")}, confirmation.get("samples")
        )
    if kind == "above_rated_speed":
        entry = {"mmo": block.get("mmo")}
        if block.get("limit_name") in SPEED_FIELDS:
            entry[block["limit_name"]] = block.get("limit")
        return speed_comparison(entry, block.get("samples"))
    return None

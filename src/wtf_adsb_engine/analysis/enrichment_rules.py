# SPDX-License-Identifier: MIT
"""What the enrichment workers' evidence names and how it is read; pure rules, no network.

typecheck.py asks two type registries and wind.py asks Open-Meteo, both through
registry_http. The story and facts layers only read what those workers wrote: the
registries' names and pages, the kinds whose premise depends on the listed type, a
type check's state, and the wind source and its attribution. They live here rather
than in the workers, so the facts layer never loads the registry client or urllib's
HTTP client. typecheck and wind import them from here.
"""

from . import kinds
from .reference import clean

# The registries typecheck.lookup() asks, in its order (typecheck.SOURCES), and their pages.
ADSBDB = "https://api.adsbdb.com/v0/aircraft/"
HEXDB = "https://hexdb.io/api/v1/aircraft/"
# Their names, as evidence.type_check.source and the public packet record them.
REGISTRIES = frozenset({"adsbdb", "hexdb"})
# Kinds whose story premise depends on the listed type being right.
TYPE_DEPENDENT_KINDS = kinds.where(type_dependent=True)
# Kinds whose story may name a registry's differing type instead of being blocked by it.
REGISTRY_TYPE_KINDS = kinds.where(registry_type=True)
# A compared check's states; "pending" (no attempt yet) and "failed" (the registries
# could not be asked; retried after the backoff) are the others.
SETTLED = frozenset({"unavailable", "agrees", "disagrees"})
# The wind source wind.py asks, and the attribution its licence asks for.
WIND_SOURCE = "open-meteo"
WIND_ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"


def type_check_state(evidence):
    """pending, failed, unavailable, agrees or disagrees.

    A check compared with a type the aircraft no longer lists is pending again,
    so a listing changed after the lookup never keeps a stale verdict (the
    worker checks it again), and so is a block in no known state.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    check = evidence.get("type_check")
    if not isinstance(check, dict) or not check:
        return "pending"
    state = check.get("state")
    if state == "failed":
        return "failed"
    aircraft = evidence.get("aircraft") if isinstance(evidence.get("aircraft"), dict) else {}
    listed = check.get("listed_type")
    if isinstance(listed, str) and listed != clean(aircraft.get("t")):
        return "pending"
    return state if state in SETTLED else "pending"

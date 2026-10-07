"""Merge duplicate listings: the same show listed twice on a site, or on several sites."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from .models import Event, platform_rank
from .normalize import title_tokens, titles_match


def _dates_compatible(a: Event, b: Event) -> bool:
    if not a.start or not b.start:
        return False
    a1, a2 = a.start.date(), (a.end or a.start).date()
    b1, b2 = b.start.date(), (b.end or b.start).date()
    if a1 == b1:
        return True
    # one listing gives the whole run, the other a single show inside it
    return (a1 <= b1 <= a2 and (a2 - a1).days <= 120) or (b1 <= a1 <= b2 and (b2 - b1).days <= 120)


def _venues_compatible(a: Event, b: Event) -> bool:
    ta, tb = title_tokens(a.venue), title_tokens(b.venue)
    return not ta or not tb or bool(ta & tb)


def same_event(a: Event, b: Event) -> bool:
    if a.url and a.url == b.url:
        return True
    return (a.city == b.city and _dates_compatible(a, b) and _venues_compatible(a, b)
            and titles_match(a.title, b.title))


def dedupe(events: List[Event]) -> List[Event]:
    """Collapse duplicates; the copy from the highest-priority platform is kept as primary."""
    by_identity: Dict[str, Event] = {}
    for ev in events:
        key = ev.identity()
        if key in by_identity:
            by_identity[key].absorb(ev)
        else:
            by_identity[key] = ev

    ordered = sorted(by_identity.values(), key=lambda e: (platform_rank(e.platform), not e.detail_fetched))
    kept: List[Event] = []
    by_city: Dict[str, List[Event]] = defaultdict(list)
    for ev in ordered:
        match = next((k for k in by_city[ev.city] if same_event(k, ev)), None)
        if match is None:
            kept.append(ev)
            by_city[ev.city].append(ev)
        else:
            match.absorb(ev)
    return kept

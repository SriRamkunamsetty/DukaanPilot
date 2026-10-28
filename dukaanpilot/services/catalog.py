"""Fuzzy product lookup that survives voice transcription and mixed languages.

Owners say "palu" or "doodh" or "milk packet"; the catalog stores one product with
a few aliases. Matching is deliberately conservative: when two products are about
equally likely we return both and let the agent ask, instead of guessing and
corrupting stock.
"""

from __future__ import annotations

import unicodedata

from ..data.models import Product


def normalize(text: str) -> str:
    """Lowercase and keep letters/marks/digits of any script (Telugu, Hindi, ...)."""
    out = []
    for ch in unicodedata.normalize("NFKC", text or "").lower():
        cat = unicodedata.category(ch)
        out.append(ch if cat[0] in {"L", "M", "N"} else " ")
    return " ".join("".join(out).split())


def _tokens(text: str) -> set[str]:
    return set(normalize(text).split())


def score(product: Product, query: str) -> float:
    q = normalize(query)
    if not q:
        return 0.0
    names = [normalize(product.name), normalize(product.sku), *(normalize(a) for a in product.aliases)]
    if q in names:
        return 1.0
    qt = _tokens(q)
    best = 0.0
    for n in names:
        if not n:
            continue
        nt = set(n.split())
        if n in q.split() or (qt and qt <= nt):
            best = max(best, 0.9)
        elif q in n:
            best = max(best, 0.85)
        elif nt and nt <= qt:
            best = max(best, 0.9)
        else:
            inter = len(qt & nt)
            if inter:
                best = max(best, 0.8 * inter / len(qt | nt))
    return best


def rank(products: list[Product], query: str, limit: int = 5, floor: float = 0.3) -> list[tuple[Product, float]]:
    scored = [(p, score(p, query)) for p in products]
    scored = [x for x in scored if x[1] >= floor]
    scored.sort(key=lambda x: (-x[1], x[0].name))
    return scored[:limit]


def resolve(products: list[Product], query: str) -> tuple[Product | None, list[Product]]:
    """Return ``(product, [])`` when confident, else ``(None, candidates)``."""
    ranked = rank(products, query)
    if not ranked:
        return None, []
    top, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if top_score >= 0.85 and (top_score - runner_up) >= 0.1:
        return top, []
    return None, [p for p, _ in ranked]

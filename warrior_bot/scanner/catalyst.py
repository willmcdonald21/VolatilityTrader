from __future__ import annotations

from dataclasses import dataclass

# IBKR news headlines are free text with no structured category -- this is a
# best-effort keyword classifier, not a guarantee. Order matters: the first
# category with a matching keyword wins, so more specific/high-value
# categories are listed first. "insider_buying" in particular is unreliable
# on general news wires (Form 4 filings aren't routinely covered as
# headlines) -- it's included so we catch it when it DOES show up as a
# story, not as a real detector for insider activity.
CATALYST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fda": ("fda", "clinical trial", "phase 1", "phase 2", "phase 3", "clearance", "emergency use authorization"),
    "merger": ("merger", "acquisition", "to be acquired", "acquires", "buyout", "definitive agreement"),
    "earnings": ("earnings", "eps", "quarterly results", "reports revenue", "guidance", "beats estimates"),
    "contract": ("contract award", "wins contract", "new contract", "partnership", "collaboration agreement"),
    "insider_buying": ("insider buying", "director buys", "ceo buys", "insider purchase", "10b5-1"),
    "upgrade": ("upgraded", "price target raised", "initiates coverage", "outperform"),
    # A recent reverse split is itself treated as a soft bullish factor in
    # the source material (artificially shrinks float, "bonus points" per
    # Ross's own five-pillars framing) -- distinct from the other
    # categories above, which are fundamental/analyst events.
    "reverse_split": ("reverse split", "reverse stock split"),
}


@dataclass(frozen=True)
class CatalystInfo:
    category: str | None
    headline: str | None

    @property
    def has_catalyst(self) -> bool:
        return self.category is not None


def classify_headlines(headlines: list[str]) -> CatalystInfo:
    """First headline that matches any category keyword wins. Returns an
    empty CatalystInfo (has_catalyst=False) if nothing matches or the list
    is empty -- callers must treat that as "unknown", never as a rejection."""
    for headline in headlines:
        lower = headline.lower()
        for category, keywords in CATALYST_KEYWORDS.items():
            if any(keyword in lower for keyword in keywords):
                return CatalystInfo(category=category, headline=headline)
    return CatalystInfo(category=None, headline=None)

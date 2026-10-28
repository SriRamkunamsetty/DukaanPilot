"""Festival calendar used for demand uplift and "what is coming" nudges."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "festivals.json"


@dataclass(frozen=True)
class Festival:
    name: str
    day: date
    lead_days: int
    uplift: float

    def window_contains(self, d: date) -> bool:
        return self.day - timedelta(days=self.lead_days) <= d <= self.day


class FestivalCalendar:
    def __init__(self, festivals: list[Festival] | None = None) -> None:
        self._festivals = sorted(festivals or [], key=lambda f: f.day)

    @classmethod
    def load(cls, path: Path | str | None = None) -> FestivalCalendar:
        raw = json.loads(Path(path or _DEFAULT_PATH).read_text(encoding="utf-8"))
        return cls(
            [
                Festival(
                    name=f["name"],
                    day=date.fromisoformat(f["date"]),
                    lead_days=int(f.get("lead_days", 5)),
                    uplift=float(f.get("uplift", 1.2)),
                )
                for f in raw.get("festivals", [])
            ]
        )

    def uplift(self, d: date) -> float:
        """Demand multiplier for a day (the strongest festival window wins)."""
        return max((f.uplift for f in self._festivals if f.window_contains(d)), default=1.0)

    def active_on(self, d: date) -> Festival | None:
        hits = [f for f in self._festivals if f.window_contains(d)]
        return max(hits, key=lambda f: f.uplift) if hits else None

    def upcoming(self, today: date, within_days: int = 30) -> list[tuple[Festival, int]]:
        """Festivals from today up to ``within_days`` away, with days remaining."""
        out = [(f, (f.day - today).days) for f in self._festivals if 0 <= (f.day - today).days <= within_days]
        return sorted(out, key=lambda x: x[1])

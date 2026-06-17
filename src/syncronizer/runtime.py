"""Per-cycle statistics accumulators (failure-isolation reporting)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class EndpointStats:
    name: str
    extracted: int = 0
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    sent: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class CycleStats:
    endpoints: Dict[str, EndpointStats] = field(default_factory=dict)
    firebird_available: bool = True

    def endpoint(self, name: str) -> EndpointStats:
        if name not in self.endpoints:
            self.endpoints[name] = EndpointStats(name=name)
        return self.endpoints[name]

    def totals(self) -> Dict[str, int]:
        keys = ("extracted", "inserted", "changed", "unchanged", "deleted", "sent", "failed")
        out = {k: 0 for k in keys}
        out["errors"] = 0
        for stats in self.endpoints.values():
            for k in keys:
                out[k] += getattr(stats, k)
            out["errors"] += len(stats.errors)
        return out

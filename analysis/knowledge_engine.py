"""Knowledge Selection Engine for Trading Intelligence V2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class KnowledgeSelection:
    selected_concepts: list[str]
    rejected_concepts: list[str]
    conflicts: list[str]
    complements: list[str]
    evidence_summary: str


class KnowledgeSelectionEngine:
    """Determines which knowledge is relevant to the current context."""
    
    @staticmethod
    def select_knowledge(
        market_state: dict[str, Any],
        instrument_dna: dict[str, Any],
        knowledge_library: list[dict[str, Any]]
    ) -> KnowledgeSelection:
        regime = market_state.get("regime", "UNKNOWN")
        selected = []
        rejected = []
        conflicts = []
        complements = []
        
        # Filter knowledge by regime and instrument DNA
        for concept in knowledge_library:
            cid = concept["knowledge_id"]
            
            # Check applicability
            if regime not in concept.get("applicability", []):
                rejected.append(cid)
                continue
                
            # Check conflicts
            conflict_found = False
            for other in selected:
                if other in concept.get("conflicts", []):
                    conflicts.append(f"{cid} conflicts with {other}")
                    conflict_found = True
                    break
            
            if conflict_found:
                rejected.append(cid)
                continue
                
            selected.append(cid)
            
            # Check complements
            for other in selected:
                if other in concept.get("complements", []):
                    complements.append(f"{cid} complements {other}")
                    
        return KnowledgeSelection(
            selected_concepts=selected,
            rejected_concepts=rejected,
            conflicts=conflicts,
            complements=complements,
            evidence_summary=f"Selected {len(selected)} concepts for regime {regime}"
        )

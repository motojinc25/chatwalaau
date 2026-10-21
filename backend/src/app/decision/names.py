"""Decision tool name (PRP-0219, UDR-0201 D3).

A dependency-free module so the tool inventory, the tool surface and the tests can name
the tool without importing the HTTP client. Named category + verb (UDR-0169 D6).
"""

DECISION_EVALUATE_TOOL = "decision_evaluate"

#: The tool category in the inventory and the guidance key.
DECISION_CATEGORY = "decision"

__all__ = ["DECISION_CATEGORY", "DECISION_EVALUATE_TOOL"]

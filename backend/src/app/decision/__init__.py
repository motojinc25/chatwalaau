"""Typed decisions with Microsoft-Decision-1 (PRP-0219, FEAT-0077, UDR-0201).

Modules:

* ``names``  -- the tool name (dependency-free, CTR-0248)
* ``schema`` -- argument validation and the wire -> normalized answer mapping (CTR-0248 / CTR-0249)
* ``client`` -- the HTTPS client for ``/providers/microsoft/v1/systemone`` (CTR-0249)
* ``tools``  -- the ``decision_evaluate`` agent tool (CTR-0248)

The client is independent of the tool (UDR-0201 D4) so a later workflow node or task
role can call the decision model without the agent.
"""

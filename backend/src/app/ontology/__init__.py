"""Ontology Concept Modeling subsystem (PRP-0105, FEAT-0058, UDR-0084).

Rule-based concept models as RDF knowledge graphs, developed for the
CONCEPT-MODEL domain only (the operator's RDF-vs-Property-Graph domain split;
runtime/instance data is out of scope):

- ``vocabulary``: the CTR-0169 meta-vocabulary codec (JSON graph projection
  <-> Turtle via the ``cw:`` application namespace).
- ``store``: the CTR-0170 Turtle-file store + ``catalog.json`` registry with a
  tolerant self-healing reader, backup-then-atomic-replace writes, and a
  READ-ONLY pyoxigraph query executor.
- ``nl``: the schema-aware NL -> SPARQL conversion through the registry
  chokepoint (CTR-0102; DEMO_MODE honored).
- ``router``: the CTR-0171 REST management API (inert 404 unless
  ONTOLOGY_ENABLED; mutations and query POSTs consume CTR-0083).
- ``tool``: the CTR-0172 session-common ``query_ontology`` agent tool
  (catalog + CONSTRUCT-only query answering fenced Turtle).

pyoxigraph is imported lazily inside the submodules so the ONTOLOGY_ENABLED
gate bounds its blast radius (UDR-0084 D2).
"""

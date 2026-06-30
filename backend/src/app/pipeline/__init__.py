"""Pipeline Job subsystem (FEAT-0021, PRP-0096, UDR-0074).

In-process, file-backed pipeline job engine that mirrors app.cron structurally:

    engine.py    in-process asyncio job queue + worker pool + cooperative cancel (CTR-0073)
    registry.py  the extensible job-type registry (runner + params schema + metadata)
    store.py     per-job JSON + run history with log capture (CTR-0145)
    router.py    /api/pipeline/* REST management API (CTR-0146)
    tool.py      manage_pipeline MAF Function Tool (CTR-0147)

Unlike the Cron Scheduler (arbitrary workspace scripts; CODING_ENABLED + jail; off by
default), pipeline jobs run only CURATED in-process job types (no shell, no
CODING_ENABLED) and the subsystem is ON by default (UDR-0074 D2/D8). rag-ingest
(FEAT-0022, CTR-0076) is the first registered job type.
"""

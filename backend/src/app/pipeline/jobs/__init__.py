"""Pipeline job-type runners (CTR-0073, PRP-0096).

Each module here implements one job type's async runner with the signature
``async def run(job, store, cancel_event) -> None``. Runners are registered into
the engine via ``app.pipeline.registry`` (the single extension point, UDR-0074 D7).
"""

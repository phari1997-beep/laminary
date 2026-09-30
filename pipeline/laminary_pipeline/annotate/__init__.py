"""LLM annotation of plot summaries (docs/PLAN.md section 5.4, docs/NARRATIVE_SCHEMA.md).

Modules: ``inputs`` (plot files and the fail-closed gate), ``prompt`` (versioned prompt and
request builder), ``client`` (Anthropic SDK wrapper), ``records`` (response -> validated record
or failure), ``runner`` (runs, idempotency, batch lifecycle), ``cost`` (offline estimates),
``config`` (settings and the unconfirmed price table).
CLI: ``python -m laminary_pipeline.annotate``.
"""

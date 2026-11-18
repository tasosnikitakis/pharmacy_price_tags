"""Application services: the database-facing side of the domain and renderer.

Services load rows, call ``core.domain`` rules and write the results. They take a
``Session`` and never commit; the caller (a router or a job) owns the transaction.
"""

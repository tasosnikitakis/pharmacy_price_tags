"""Database-facing services: load rows, call ``core.domain`` rules, write the results.

Services take a ``Session`` and never commit; the caller (a router or a job) owns the
transaction.
"""

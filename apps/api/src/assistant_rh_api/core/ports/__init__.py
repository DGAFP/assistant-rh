"""Narrow async store boundaries; no SQL, provider SDKs or transport types.

Read methods distinguish absence (None) from failure (ApplicationError).
Adapters return immutable snapshots and keep cache/lifecycle state outside core.
Runtime repositories consume domain values and return explicit records.
Provider contracts carry immutable outcomes and per-call diagnostics.
"""

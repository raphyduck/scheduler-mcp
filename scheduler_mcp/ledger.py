"""Ledger SQLite (BUILD_BRIEF.md commit 2).

Tables jobs et runs, mode WAL, idempotence par (job_id, scheduled_for),
verrou par job (lock_owner / lock_expires). A implementer.
"""

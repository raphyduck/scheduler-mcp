"""Tests du ledger SQLite (BUILD_BRIEF.md commit 2).

Autonome : ne depend que de la stdlib et d'aiosqlite, executable via
    python -m tests.test_ledger
Verifie le mode WAL, les migrations, l'idempotence par (job_id, scheduled_for)
et le verrou par job (anti double-dispatch).
"""

import asyncio
import tempfile
from pathlib import Path

from scheduler_mcp.ledger import Ledger, now_iso


async def _seed_active_job(ledger: Ledger) -> int:
    """Cree un job actif et du (next_run dans le passe)."""
    return await ledger.upsert_job(
        notion_page_id="page-1",
        nom="Rappel test",
        type="notification",
        schedule="*/5 * * * *",
        payload={"message": "coucou", "canal": "email"},
        toolset=["imap"],
        statut="actif",
        classif_reason="rappel simple",
        next_run="2020-01-01T00:00:00.000000Z",
    )


async def test_wal_mode(ledger: Ledger) -> None:
    cur = await ledger.db.execute("PRAGMA journal_mode")
    mode = (await cur.fetchone())[0]
    assert mode.lower() == "wal", f"journal_mode attendu wal, obtenu {mode}"


async def test_migrations_idempotentes(path: str) -> None:
    # Une seconde connexion ne doit pas rejouer les migrations.
    async with Ledger(path) as second:
        cur = await second.db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == 1
        cur = await second.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {r[0] for r in await cur.fetchall()}
        assert {"jobs", "runs"} <= tables, tables


async def test_upsert_roundtrip(ledger: Ledger) -> None:
    job_id = await _seed_active_job(ledger)
    job = await ledger.get_job(job_id)
    assert job["nom"] == "Rappel test"
    assert job["payload"] == {"message": "coucou", "canal": "email"}
    assert job["toolset"] == ["imap"]
    # Upsert sur le meme notion_page_id : meme ligne, champ declaratif maj.
    again = await ledger.upsert_job(
        notion_page_id="page-1", nom="Rappel renomme", type="notification", statut="actif",
    )
    assert again == job_id
    assert (await ledger.get_job(job_id))["nom"] == "Rappel renomme"
    # next_run non fourni : preserve.
    assert (await ledger.get_job(job_id))["next_run"] == "2020-01-01T00:00:00.000000Z"


async def test_due_jobs(ledger: Ledger) -> None:
    job_id = await _seed_active_job(ledger)
    due = await ledger.due_jobs(now=now_iso())
    assert [j["id"] for j in due] == [job_id]
    # Un job en pause n'est pas du.
    await ledger.upsert_job(
        notion_page_id="page-2", nom="En pause", type="notification",
        statut="en pause", next_run="2020-01-01T00:00:00.000000Z",
    )
    due = await ledger.due_jobs(now=now_iso())
    assert [j["id"] for j in due] == [job_id]


async def test_lock_anti_double_dispatch(ledger: Ledger) -> None:
    job_id = await _seed_active_job(ledger)
    assert await ledger.acquire_lock(job_id, owner="worker-a") is True
    # Un second worker ne peut pas prendre un verrou vivant.
    assert await ledger.acquire_lock(job_id, owner="worker-b") is False
    # Le job verrouille sort de la selection des jobs dus.
    assert await ledger.due_jobs(now=now_iso()) == []
    # Liberation : le verrou redevient disponible.
    await ledger.release_lock(job_id, owner="worker-a")
    assert await ledger.acquire_lock(job_id, owner="worker-b") is True


async def test_lock_expire(ledger: Ledger) -> None:
    job_id = await _seed_active_job(ledger)
    # TTL negatif : verrou immediatement expire, donc reprenable.
    assert await ledger.acquire_lock(job_id, owner="worker-a", ttl_seconds=-1) is True
    assert await ledger.acquire_lock(job_id, owner="worker-b") is True


async def test_idempotence(ledger: Ledger) -> None:
    job_id = await _seed_active_job(ledger)
    slot = "2020-01-01T00:00:00.000000Z"
    run_id = await ledger.start_run(job_id, slot)
    assert run_id is not None
    # Meme creneau : pas de second run, le job n'est pas rejoue.
    assert await ledger.start_run(job_id, slot) is None
    assert await ledger.run_exists(job_id, slot) is True
    # Un autre creneau cree bien un nouveau run.
    other = await ledger.start_run(job_id, "2020-01-01T00:05:00.000000Z")
    assert other is not None and other != run_id


async def test_finish_run_reporte_sur_job(ledger: Ledger) -> None:
    job_id = await _seed_active_job(ledger)
    run_id = await ledger.start_run(job_id, "2020-01-01T00:00:00.000000Z")
    await ledger.finish_run(run_id, result="success", detail="message envoye")
    run = await ledger.get_run(run_id)
    assert run["result"] == "success"
    assert run["finished_at"] is not None
    job = await ledger.get_job(job_id)
    assert job["last_result"] == "success"
    assert job["last_run"] is not None


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "scheduler.db")

        # Tests qui exigent une base fraiche, chacun sur sa propre connexion.
        async def with_fresh(coro) -> None:
            import os
            for ext in ("", "-wal", "-shm"):
                try:
                    os.remove(path + ext)
                except FileNotFoundError:
                    pass
            async with Ledger(path) as ledger:
                await coro(ledger)

        await with_fresh(test_wal_mode)
        await test_migrations_idempotentes(path)
        await with_fresh(test_upsert_roundtrip)
        await with_fresh(test_due_jobs)
        await with_fresh(test_lock_anti_double_dispatch)
        await with_fresh(test_lock_expire)
        await with_fresh(test_idempotence)
        await with_fresh(test_finish_run_reporte_sur_job)

    print("OK : tous les tests du ledger passent")


if __name__ == "__main__":
    asyncio.run(main())

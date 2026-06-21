"""Boucle de tick (BUILD_BRIEF.md commit 4).

Toutes les TICK_INTERVAL_SECONDS : lire les jobs dus (next_run echue, statut
actif, y compris les jobs en retard) et les dispatcher vers un pool de workers
borne (semaphore = MAX_CONCURRENT_RUNS). La verite reste dans SQLite ; la boucle
ne garde aucun etat en memoire.

Deux garde-fous se completent :
- verrou par job (acquire_lock) : un seul worker prend un job a la fois, meme si
  un tick suivant arrive avant la fin du precedent ou apres un redemarrage ;
- idempotence (start_run sur (job_id, scheduled_for)) : un creneau deja execute
  n'est jamais rejoue.

Apres execution, next_run est avance au prochain creneau futur (rattrapage en une
fois : on ne rejoue pas tous les creneaux manques pendant un downtime).
"""

import asyncio
import os
import socket
import uuid
from typing import Optional

from .config import Config
from .journal import Journal
from .ledger import Ledger, now_iso
from .logging_conf import get_logger
from .executors.base import Dispatcher, RunResult
from .notion_sync import compute_next_run

log = get_logger("scheduler_mcp.tick")


def make_owner() -> str:
    """Identite unique de cette instance pour le verrou (host:pid:rand)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def _advance_next_run(ledger: Ledger, job: dict) -> None:
    """Avance next_run au prochain creneau futur apres execution du creneau courant.

    Ancre sur maintenant : un cron saute au prochain creneau a venir (le creneau
    en retard a deja ete execute une fois), un one-shot n'a plus de next_run.
    """
    now = now_iso()
    next_run = compute_next_run(job.get("schedule"), now, now)
    await ledger.set_next_run(job["id"], next_run)


async def _run_job(
    ledger: Ledger,
    dispatcher: Dispatcher,
    sem: asyncio.Semaphore,
    owner: str,
    job: dict,
    journal: Optional[Journal] = None,
) -> None:
    """Execute un job verrouille : idempotence, dispatch borne, cloture, Journal, avance.

    Le verrou est suppose deja acquis par l'appelant ; il est toujours libere ici.
    """
    job_id = job["id"]
    scheduled_for = job["next_run"]
    try:
        run_id = await ledger.start_run(job_id, scheduled_for)
        if run_id is None:
            # Creneau deja reclame/execute : on ne rejoue pas, on avance seulement.
            log.info("tick.idempotent_skip", job=job_id, slot=scheduled_for)
        else:
            async with sem:
                log.info("tick.dispatch", job=job_id, type=job.get("type"), slot=scheduled_for)
                result = await _safe_dispatch(dispatcher, job)
            await ledger.finish_run(run_id, result.result, result.detail)
            log.info("tick.finished", job=job_id, result=result.result)
            await _record_journal(ledger, journal, run_id, job, result)
        await _advance_next_run(ledger, job)
    finally:
        await ledger.release_lock(job_id, owner)


async def _record_journal(
    ledger: Ledger, journal: Optional[Journal], run_id: int, job: dict, result: RunResult
) -> None:
    """Append le run au Journal Notion et memorise la page dans runs (best effort)."""
    if journal is None or not journal.enabled:
        return
    page_id = await journal.record(
        nom=job.get("nom") or "",
        type_=job.get("type") or "",
        result=result.result,
        detail=result.detail or "",
    )
    if page_id:
        await ledger.set_run_journal(run_id, page_id)


async def _safe_dispatch(dispatcher: Dispatcher, job: dict) -> RunResult:
    """Isole l'executor : une exception devient un resultat failure, pas un crash."""
    try:
        return await dispatcher.dispatch(job)
    except Exception as exc:
        log.error("tick.dispatch_error", job=job["id"], error=str(exc), type=type(exc).__name__)
        return RunResult.fail(f"exception {type(exc).__name__}: {exc}")


async def tick_once(
    cfg: Config,
    ledger: Ledger,
    dispatcher: Dispatcher,
    sem: asyncio.Semaphore,
    owner: str,
    now: Optional[str] = None,
    journal: Optional[Journal] = None,
) -> list[asyncio.Task]:
    """Un tick : selectionne les jobs dus, verrouille, lance un worker par job.

    Retourne les taches creees (non attendues ici, pour ne pas bloquer la cadence
    du tick) ; l'appelant en gere le cycle de vie. Les tests peuvent les attendre.
    """
    now = now or now_iso()
    due = await ledger.due_jobs(now)
    tasks: list[asyncio.Task] = []
    for job in due:
        if not await ledger.acquire_lock(job["id"], owner, cfg.lock_ttl_seconds, now):
            # Verrouille par une autre execution : pas de double dispatch.
            log.info("tick.locked_skip", job=job["id"])
            continue
        tasks.append(
            asyncio.create_task(_run_job(ledger, dispatcher, sem, owner, job, journal))
        )
    if tasks:
        log.info("tick.dispatched", count=len(tasks))
    return tasks


async def tick_loop(
    cfg: Config, ledger: Ledger, dispatcher: Dispatcher, journal: Optional[Journal] = None
) -> None:
    """Boucle perpetuelle : un tick toutes les TICK_INTERVAL_SECONDS."""
    sem = asyncio.Semaphore(cfg.max_concurrent_runs)
    owner = make_owner()
    pending: set[asyncio.Task] = set()
    log.info("tick.loop_start", owner=owner, max_concurrent=cfg.max_concurrent_runs)
    while True:
        try:
            tasks = await tick_once(cfg, ledger, dispatcher, sem, owner, journal=journal)
            for task in tasks:
                pending.add(task)
                task.add_done_callback(pending.discard)
        except Exception as exc:  # un tick ne doit jamais tuer la boucle.
            log.error("tick.error", error=str(exc), type=type(exc).__name__)
        await asyncio.sleep(cfg.tick_interval_seconds)

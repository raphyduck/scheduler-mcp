"""Tests de la boucle de tick (BUILD_BRIEF.md commit 4).

Autonome : executors simules, ledger ephemere.
    python -m tests.test_tick
Verifie le dispatch des jobs dus, l'idempotence (pas de rejeu), le verrou
anti double-dispatch, l'isolement des erreurs d'executor, l'avance de next_run
et le respect de la borne de concurrence.
"""

import asyncio
import tempfile
from pathlib import Path

from scheduler_mcp.config import Config
from scheduler_mcp.executors.base import Dispatcher, RunResult
from scheduler_mcp.ledger import Ledger, now_iso, parse_iso
from scheduler_mcp.tick import tick_once

PAST = "2020-01-01T00:00:00.000000Z"


def make_cfg(max_concurrent=4, lock_ttl=900) -> Config:
    return Config(
        anthropic_api_key="", notion_token="", notion_version="2025-09-03",
        notion_programmation_ds="", notion_journal_db="", sqlite_path=":memory:",
        tick_interval_seconds=60, notion_sync_interval_seconds=300,
        max_concurrent_runs=max_concurrent, lock_ttl_seconds=lock_ttl,
        llm_model="x", log_level="INFO",
    )


class RecordingExecutor:
    """Executor qui enregistre les jobs recus et retourne un resultat fixe."""

    def __init__(self, result: RunResult = None) -> None:
        self.calls: list[int] = []
        self._result = result or RunResult.ok("fait")

    async def execute(self, job: dict) -> RunResult:
        self.calls.append(job["id"])
        return self._result


class RaisingExecutor:
    async def execute(self, job: dict) -> RunResult:
        raise RuntimeError("boom")


class SlowExecutor:
    """Mesure le pic de concurrence observe pendant les executions."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def execute(self, job: dict) -> RunResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return RunResult.ok()


async def _seed(ledger: Ledger, notion_id: str, *, schedule="*/5 * * * *",
                statut="actif", next_run=PAST, type_="notification") -> int:
    return await ledger.upsert_job(
        notion_page_id=notion_id, nom=notion_id, type=type_,
        schedule=schedule, statut=statut, next_run=next_run,
    )


async def test_dispatch_et_cloture(ledger: Ledger) -> None:
    cfg = make_cfg()
    execu = RecordingExecutor()
    disp = Dispatcher({"notification": execu})
    job_id = await _seed(ledger, "job-1")

    tasks = await tick_once(cfg, ledger, disp, asyncio.Semaphore(4), "owner-a")
    await asyncio.gather(*tasks)

    assert execu.calls == [job_id]
    assert await ledger.run_exists(job_id, PAST) is True
    job = await ledger.get_job(job_id)
    assert job["last_result"] == "success"
    # next_run avance dans le futur ; le verrou est libere.
    assert parse_iso(job["next_run"]) > parse_iso(now_iso())
    assert job["lock_owner"] is None


async def test_pas_de_rejeu_apres_avance(ledger: Ledger) -> None:
    cfg = make_cfg()
    execu = RecordingExecutor()
    disp = Dispatcher({"notification": execu})
    await _seed(ledger, "job-1")

    await asyncio.gather(*await tick_once(cfg, ledger, disp, asyncio.Semaphore(4), "o"))
    # Deuxieme tick : next_run est desormais futur, plus rien de du.
    tasks2 = await tick_once(cfg, ledger, disp, asyncio.Semaphore(4), "o")
    await asyncio.gather(*tasks2)
    assert len(execu.calls) == 1


async def test_idempotence_creneau_deja_reclame(ledger: Ledger) -> None:
    cfg = make_cfg()
    execu = RecordingExecutor()
    disp = Dispatcher({"notification": execu})
    job_id = await _seed(ledger, "job-1")
    # Le creneau a deja un run (ex. execute avant un redemarrage).
    assert await ledger.start_run(job_id, PAST) is not None

    tasks = await tick_once(cfg, ledger, disp, asyncio.Semaphore(4), "o")
    await asyncio.gather(*tasks)
    # Pas de rejeu, mais next_run avance et verrou libere.
    assert execu.calls == []
    job = await ledger.get_job(job_id)
    assert parse_iso(job["next_run"]) > parse_iso(now_iso())
    assert job["lock_owner"] is None


async def test_verrou_anti_double_dispatch(ledger: Ledger) -> None:
    cfg = make_cfg()
    execu = RecordingExecutor()
    disp = Dispatcher({"notification": execu})
    job_id = await _seed(ledger, "job-1")
    # Un autre worker detient le verrou.
    assert await ledger.acquire_lock(job_id, "autre-worker", 900) is True

    tasks = await tick_once(cfg, ledger, disp, asyncio.Semaphore(4), "moi")
    await asyncio.gather(*tasks)
    assert execu.calls == []
    assert tasks == []


async def test_executor_en_erreur(ledger: Ledger) -> None:
    cfg = make_cfg()
    disp = Dispatcher({"notification": RaisingExecutor()})
    job_id = await _seed(ledger, "job-1")

    tasks = await tick_once(cfg, ledger, disp, asyncio.Semaphore(4), "o")
    await asyncio.gather(*tasks)
    job = await ledger.get_job(job_id)
    # L'exception devient un run failure ; le service ne crashe pas.
    assert job["last_result"] == "failure"
    assert job["lock_owner"] is None
    run = (await ledger.list_runs(job_id))[0]
    assert "boom" in (run["detail"] or "")


async def test_type_sans_executor_skipped(ledger: Ledger) -> None:
    cfg = make_cfg()
    disp = Dispatcher()  # aucun executor enregistre
    job_id = await _seed(ledger, "job-1", type_="agent")

    await asyncio.gather(*await tick_once(cfg, ledger, disp, asyncio.Semaphore(4), "o"))
    job = await ledger.get_job(job_id)
    assert job["last_result"] == "skipped"


async def test_borne_de_concurrence(ledger: Ledger) -> None:
    cfg = make_cfg(max_concurrent=2)
    slow = SlowExecutor()
    disp = Dispatcher({"notification": slow})
    for i in range(5):
        await _seed(ledger, f"job-{i}")

    sem = asyncio.Semaphore(cfg.max_concurrent_runs)
    tasks = await tick_once(cfg, ledger, disp, sem, "o")
    await asyncio.gather(*tasks)
    assert len(tasks) == 5
    # Jamais plus de 2 executions simultanees.
    assert slow.peak <= 2, f"pic de concurrence {slow.peak} > 2"


async def main() -> None:
    tests = [
        test_dispatch_et_cloture,
        test_pas_de_rejeu_apres_avance,
        test_idempotence_creneau_deja_reclame,
        test_verrou_anti_double_dispatch,
        test_executor_en_erreur,
        test_type_sans_executor_skipped,
        test_borne_de_concurrence,
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for i, test in enumerate(tests):
            path = str(Path(tmp) / f"t{i}.db")
            async with Ledger(path) as ledger:
                await test(ledger)
    print("OK : tous les tests de tick passent")


if __name__ == "__main__":
    asyncio.run(main())

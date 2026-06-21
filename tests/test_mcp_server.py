"""Tests de l'interface serveur MCP (BUILD_BRIEF.md commit 10).

Autonome : TaskService adosse a un ledger ephemere, compiler simule (aucun reseau,
pas de dependance mcp). Verifie add (compilation -> ledger, echeance), list (filtre
par statut), update (statut/type/schedule, recalcul next_run), et le gate a_valider.
    python -m tests.test_mcp_server
"""

import asyncio
import tempfile
from pathlib import Path

from scheduler_mcp.compiler import CompiledJob
from scheduler_mcp.config import Config
from scheduler_mcp.ledger import Ledger, parse_iso, now_iso
from scheduler_mcp.mcp_server import TaskService


def make_cfg() -> Config:
    return Config(
        anthropic_api_key="", notion_token="", notion_version="2025-09-03",
        notion_programmation_ds="", notion_journal_db="", sqlite_path=":memory:",
        tick_interval_seconds=60, notion_sync_interval_seconds=300,
        max_concurrent_runs=4, lock_ttl_seconds=900, script_timeout_seconds=300,
        llm_model="x", llm_max_tokens=4096, log_level="INFO", mcp_servers={},
    )


class FakeCompiler:
    """Renvoie un CompiledJob preconfigure et enregistre les entrees compilees."""

    def __init__(self, job: CompiledJob) -> None:
        self._job = job
        self.calls: list[str] = []

    async def compile(self, text, nom=None) -> CompiledJob:
        self.calls.append(text)
        return self._job


def compiled(type_="notification", schedule=None, statut="actif",
             payload=None, toolset=None, sensitive=None) -> CompiledJob:
    return CompiledJob(
        type=type_, payload=payload or {"message": "x"}, toolset=toolset or [],
        classif_reason="r", statut=statut, schedule=schedule,
        sensitive_reasons=sensitive or [],
    )


def service(ledger, job) -> TaskService:
    return TaskService(make_cfg(), ledger=ledger, compiler=FakeCompiler(job))


async def test_add_cron(ledger: Ledger) -> None:
    svc = service(ledger, compiled(type_="agent", schedule="*/5 * * * *",
                                   payload={"instruction": "trie"}, toolset=["imap"]))
    res = await svc.add_task("trie mes mails toutes les 5 minutes")
    assert res["type"] == "agent"
    assert res["statut"] == "actif"
    assert res["toolset"] == ["imap"]
    assert parse_iso(res["next_run"]) > parse_iso(now_iso())
    # Persiste dans le ledger sous un id notion mcp:.
    jobs = await ledger.list_jobs()
    assert len(jobs) == 1 and jobs[0]["notion_page_id"].startswith("mcp:")


async def test_add_sans_echeance_immediate(ledger: Ledger) -> None:
    svc = service(ledger, compiled(schedule=None))
    res = await svc.add_task("previens-moi")
    # Sans schedule : next_run = maintenant (executable au prochain tick).
    assert res["next_run"] is not None
    assert parse_iso(res["next_run"]) <= parse_iso(now_iso())


async def test_add_script_sensible_a_valider(ledger: Ledger) -> None:
    svc = service(ledger, compiled(type_="script", statut="a_valider",
                                   payload={"command": "rm -rf /x"},
                                   sensitive=["suppression de fichiers (rm -f)"]))
    res = await svc.add_task("supprime tout")
    assert res["statut"] == "a_valider"
    assert res["sensitive_reasons"]
    # En a_valider, la tache n'est pas due.
    assert await ledger.due_jobs(now_iso()) == []


async def test_list_filtre_statut(ledger: Ledger) -> None:
    await service(ledger, compiled(statut="actif")).add_task("a")
    await service(ledger, compiled(type_="script", statut="a_valider",
                                   payload={"command": "echo hi"})).add_task("b")
    svc = TaskService(make_cfg(), ledger=ledger, compiler=FakeCompiler(compiled()))
    assert len(await svc.list_tasks()) == 2
    assert len(await svc.list_tasks(statut="a_valider")) == 1


async def test_update_active_et_reprogramme(ledger: Ledger) -> None:
    svc = service(ledger, compiled(type_="script", statut="a_valider",
                                   schedule="0 9 * * *", payload={"command": "echo hi"}))
    created = await svc.add_task("script du matin")
    job_id = created["id"]

    # Validation : passage en actif -> devient ordonnancable.
    updated = await svc.update_task(job_id, statut="actif")
    assert updated["statut"] == "actif"

    # Changement d'echeance : next_run recalcule.
    res = await svc.update_task(job_id, schedule="*/10 * * * *")
    assert res["schedule"] == "*/10 * * * *"
    assert parse_iso(res["next_run"]) > parse_iso(now_iso())


async def test_update_statut_invalide_et_id_inconnu(ledger: Ledger) -> None:
    svc = service(ledger, compiled())
    created = await svc.add_task("x")
    try:
        await svc.update_task(created["id"], statut="n_importe_quoi")
    except ValueError:
        pass
    else:
        raise AssertionError("statut invalide doit lever ValueError")
    try:
        await svc.update_task(99999, statut="actif")
    except ValueError:
        pass
    else:
        raise AssertionError("id inconnu doit lever ValueError")


async def test_update_schedule_vide_retire_next_run(ledger: Ledger) -> None:
    svc = service(ledger, compiled(schedule="0 9 * * *"))
    created = await svc.add_task("x")
    res = await svc.update_task(created["id"], schedule="")
    assert res["schedule"] is None
    assert res["next_run"] is None


async def main() -> None:
    tests = [
        test_add_cron,
        test_add_sans_echeance_immediate,
        test_add_script_sensible_a_valider,
        test_list_filtre_statut,
        test_update_active_et_reprogramme,
        test_update_statut_invalide_et_id_inconnu,
        test_update_schedule_vide_retire_next_run,
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for i, test in enumerate(tests):
            async with Ledger(str(Path(tmp) / f"t{i}.db")) as ledger:
                await test(ledger)
    print("OK : tous les tests de l'interface MCP passent")


if __name__ == "__main__":
    asyncio.run(main())

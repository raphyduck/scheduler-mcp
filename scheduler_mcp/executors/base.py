"""Interface commune des executors (BUILD_BRIEF.md commits 5 a 7).

Chaque executor recoit un job et retourne un resultat structure (succes/echec +
detail) qui alimente la table runs et le Journal. Le dispatcher route un job vers
l'executor correspondant a son type. Les executors concrets (notification, script,
agent) sont ajoutes aux commits 5 a 7 ; ils s'enregistrent via Dispatcher.register.
"""

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

# Valeurs canoniques du champ result de la table runs.
SUCCESS = "success"
FAILURE = "failure"
SKIPPED = "skipped"


@dataclass
class RunResult:
    """Resultat structure d'une execution."""

    result: str
    detail: Optional[str] = None

    @classmethod
    def ok(cls, detail: Optional[str] = None) -> "RunResult":
        return cls(SUCCESS, detail)

    @classmethod
    def fail(cls, detail: Optional[str] = None) -> "RunResult":
        return cls(FAILURE, detail)

    @classmethod
    def skip(cls, detail: Optional[str] = None) -> "RunResult":
        return cls(SKIPPED, detail)


@runtime_checkable
class Executor(Protocol):
    """Contrat d'un executor : prend un job (dict du ledger) et l'execute."""

    async def execute(self, job: dict) -> RunResult: ...


class Dispatcher:
    """Route un job vers l'executor enregistre pour son type.

    Un type sans executor enregistre retourne un resultat skipped explicite :
    la boucle de tick reste fonctionnelle avant l'arrivee des executors concrets.
    """

    def __init__(self, executors: Optional[dict[str, Executor]] = None) -> None:
        self._executors: dict[str, Executor] = dict(executors or {})

    def register(self, type_: str, executor: Executor) -> None:
        self._executors[type_] = executor

    async def dispatch(self, job: dict) -> RunResult:
        executor = self._executors.get(job.get("type"))
        if executor is None:
            return RunResult.skip(
                f"aucun executor pour le type {job.get('type')!r} "
                "(voir BUILD_BRIEF.md commits 5 a 7)"
            )
        return await executor.execute(job)

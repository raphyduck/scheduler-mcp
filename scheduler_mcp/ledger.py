"""Ledger SQLite (BUILD_BRIEF.md commit 2).

Couche d'acces au ledger local : tables jobs et runs, mode WAL, migrations
versionnees, idempotence par (job_id, scheduled_for) et verrou par job
(lock_owner / lock_expires) pour eviter le double dispatch.

La verite vit dans SQLite : la boucle de tick ne garde aucun etat en memoire.
Tous les horodatages sont stockes en ISO 8601 UTC (helper now_iso) pour que la
comparaison lexicographique des chaines coincide avec l'ordre chronologique.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiosqlite


# Format unique des horodatages : ISO 8601 UTC suffixe Z, longueur fixe pour que
# la comparaison de chaines en SQL reste chronologique.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


# Migrations appliquees dans l'ordre, indexees par PRAGMA user_version.
# Pour faire evoluer le schema : ajouter une entree, ne jamais editer une
# migration deja livree.
_MIGRATIONS: list[str] = [
    # version 1 : schema initial jobs + runs.
    """
    CREATE TABLE jobs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        notion_page_id  TEXT NOT NULL UNIQUE,
        nom             TEXT NOT NULL,
        type            TEXT NOT NULL,
        schedule        TEXT,
        payload         TEXT,
        toolset         TEXT,
        statut          TEXT NOT NULL DEFAULT 'a_valider',
        next_run        TEXT,
        last_run        TEXT,
        last_result     TEXT,
        classif_reason  TEXT,
        lock_owner      TEXT,
        lock_expires    TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    CREATE TABLE runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        scheduled_for   TEXT NOT NULL,
        started_at      TEXT,
        finished_at     TEXT,
        result          TEXT,
        detail          TEXT,
        journal_page_id TEXT,
        UNIQUE (job_id, scheduled_for)
    );

    CREATE INDEX idx_jobs_due ON jobs (statut, next_run);
    CREATE INDEX idx_runs_job ON runs (job_id);
    """,
]


def now_iso() -> str:
    """Horodatage courant en ISO 8601 UTC, suffixe Z."""
    return datetime.now(timezone.utc).strftime(_TS_FORMAT)


def to_iso(dt: datetime) -> str:
    """Convertit un datetime (naif suppose UTC) en ISO 8601 UTC, suffixe Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_TS_FORMAT)


# Alias interne historique.
_dt_iso = to_iso


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse un horodatage ISO 8601 (date seule, avec offset ou suffixe Z) en UTC.

    Retourne None si la valeur est vide ou non interpretable. Tolerant pour
    accepter aussi bien le format interne (now_iso) que les dates Notion.
    """
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _shift_iso(base_iso: str, seconds: int) -> str:
    """Decale un horodatage ISO de N secondes (calcul des TTL de verrou)."""
    base = datetime.strptime(base_iso, _TS_FORMAT).replace(tzinfo=timezone.utc)
    return _dt_iso(base + timedelta(seconds=seconds))


def _dumps(value: Any) -> Optional[str]:
    """Serialise payload/toolset en JSON. None reste None, str passe tel quel."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Optional[str]) -> Any:
    """Deserialise un champ JSON. None reste None, JSON invalide -> valeur brute."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _row_to_job(row: aiosqlite.Row) -> dict:
    job = dict(row)
    job["payload"] = _loads(job.get("payload"))
    job["toolset"] = _loads(job.get("toolset"))
    return job


class Ledger:
    """Acces asynchrone au ledger SQLite.

    Une seule connexion aiosqlite : aiosqlite serialise les operations sur sa
    connexion, donc les ecritures ne se chevauchent pas. L'idempotence repose
    sur la contrainte UNIQUE(job_id, scheduled_for) ; la garantie anti
    double-dispatch repose sur un UPDATE conditionnel atomique (acquire_lock).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Ledger non connecte : appeler connect() d'abord")
        return self._db

    async def connect(self) -> "Ledger":
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        # WAL : lecteurs concurrents + un ecrivain, pas de blocage mutuel.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._migrate()
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "Ledger":
        return await self.connect()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _migrate(self) -> None:
        cur = await self.db.execute("PRAGMA user_version")
        version = (await cur.fetchone())[0]
        for index in range(version, len(_MIGRATIONS)):
            await self.db.executescript(_MIGRATIONS[index])
            # PRAGMA n'accepte pas de parametre lie : la valeur vient d'un index interne.
            await self.db.execute(f"PRAGMA user_version={index + 1}")
            await self.db.commit()

    # ----- jobs (declaratif, alimente par la sync Notion) -----

    async def upsert_job(
        self,
        *,
        notion_page_id: str,
        nom: str,
        type: str,
        schedule: Optional[str] = None,
        payload: Any = None,
        toolset: Any = None,
        statut: str = "a_valider",
        classif_reason: Optional[str] = None,
        next_run: Optional[str] = None,
    ) -> int:
        """Upsert d'un job par notion_page_id.

        Met a jour les champs declaratifs (la base Programmation fait foi) sans
        ecraser l'etat d'execution (last_run, last_result, verrou). next_run
        n'est mis a jour que s'il est fourni (calcule par la sync).
        """
        ts = now_iso()
        await self.db.execute(
            """
            INSERT INTO jobs (
                notion_page_id, nom, type, schedule, payload, toolset, statut,
                classif_reason, next_run, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (notion_page_id) DO UPDATE SET
                nom            = excluded.nom,
                type           = excluded.type,
                schedule       = excluded.schedule,
                payload        = excluded.payload,
                toolset        = excluded.toolset,
                statut         = excluded.statut,
                classif_reason = excluded.classif_reason,
                next_run       = COALESCE(excluded.next_run, jobs.next_run),
                updated_at     = excluded.updated_at
            """,
            (
                notion_page_id, nom, type, schedule, _dumps(payload), _dumps(toolset),
                statut, classif_reason, next_run, ts, ts,
            ),
        )
        await self.db.commit()
        cur = await self.db.execute(
            "SELECT id FROM jobs WHERE notion_page_id = ?", (notion_page_id,)
        )
        return (await cur.fetchone())[0]

    async def get_job(self, job_id: int) -> Optional[dict]:
        cur = await self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return _row_to_job(row) if row else None

    async def get_job_by_notion(self, notion_page_id: str) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT * FROM jobs WHERE notion_page_id = ?", (notion_page_id,)
        )
        row = await cur.fetchone()
        return _row_to_job(row) if row else None

    async def list_jobs(self) -> list[dict]:
        cur = await self.db.execute("SELECT * FROM jobs ORDER BY id")
        return [_row_to_job(r) for r in await cur.fetchall()]

    async def set_next_run(self, job_id: int, next_run: Optional[str]) -> None:
        await self.db.execute(
            "UPDATE jobs SET next_run = ?, updated_at = ? WHERE id = ?",
            (next_run, now_iso(), job_id),
        )
        await self.db.commit()

    # Colonnes modifiables via update_job. payload/toolset sont serialises en JSON.
    _UPDATABLE = ("nom", "type", "schedule", "statut", "classif_reason", "next_run")
    _UPDATABLE_JSON = ("payload", "toolset")

    async def update_job(self, job_id: int, **changes: Any) -> bool:
        """Met a jour les colonnes fournies d'un job. Retourne True si le job existe."""
        cols: list[str] = []
        params: list[Any] = []
        for key in self._UPDATABLE:
            if key in changes:
                cols.append(f"{key} = ?")
                params.append(changes[key])
        for key in self._UPDATABLE_JSON:
            if key in changes:
                cols.append(f"{key} = ?")
                params.append(_dumps(changes[key]))
        if not cols:
            return await self.get_job(job_id) is not None
        params.append(now_iso())
        params.append(job_id)
        cur = await self.db.execute(
            f"UPDATE jobs SET {', '.join(cols)}, updated_at = ? WHERE id = ?", params
        )
        await self.db.commit()
        return cur.rowcount == 1

    # ----- selection des jobs dus (boucle de tick) -----

    async def due_jobs(self, now: Optional[str] = None) -> list[dict]:
        """Jobs dus : statut actif, next_run echue (incl. retard), libres de verrou.

        Le filtre sur le verrou est opportuniste ; la garantie anti double-dispatch
        vient de acquire_lock (UPDATE conditionnel atomique).
        """
        now = now or now_iso()
        cur = await self.db.execute(
            """
            SELECT * FROM jobs
            WHERE statut = 'actif'
              AND next_run IS NOT NULL
              AND next_run <= ?
              AND (lock_owner IS NULL OR lock_expires IS NULL OR lock_expires <= ?)
            ORDER BY next_run
            """,
            (now, now),
        )
        return [_row_to_job(r) for r in await cur.fetchall()]

    # ----- verrou par job (anti double-dispatch) -----

    async def acquire_lock(
        self, job_id: int, owner: str, ttl_seconds: int = 300, now: Optional[str] = None
    ) -> bool:
        """Tente de prendre le verrou. Atomique : un seul worker l'emporte.

        Reussit si le job n'est pas verrouille ou si le verrou a expire.
        Retourne True si le verrou est acquis.
        """
        now = now or now_iso()
        expires = _shift_iso(now, ttl_seconds)
        cur = await self.db.execute(
            """
            UPDATE jobs
            SET lock_owner = ?, lock_expires = ?, updated_at = ?
            WHERE id = ?
              AND (lock_owner IS NULL OR lock_expires IS NULL OR lock_expires <= ?)
            """,
            (owner, expires, now, job_id, now),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def release_lock(self, job_id: int, owner: str) -> None:
        """Libere le verrou si l'appelant le detient encore."""
        await self.db.execute(
            """
            UPDATE jobs
            SET lock_owner = NULL, lock_expires = NULL, updated_at = ?
            WHERE id = ? AND lock_owner = ?
            """,
            (now_iso(), job_id, owner),
        )
        await self.db.commit()

    # ----- runs (audit + idempotence) -----

    async def start_run(
        self, job_id: int, scheduled_for: str, now: Optional[str] = None
    ) -> Optional[int]:
        """Reclame un creneau d'execution. Cle d'idempotence (job_id, scheduled_for).

        Retourne l'id du run cree, ou None si un run existe deja pour ce creneau
        (job deja fait ou en cours) : il ne sera pas rejoue.
        """
        try:
            cur = await self.db.execute(
                "INSERT INTO runs (job_id, scheduled_for, started_at) VALUES (?, ?, ?)",
                (job_id, scheduled_for, now or now_iso()),
            )
            await self.db.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            await self.db.rollback()
            return None

    async def run_exists(self, job_id: int, scheduled_for: str) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM runs WHERE job_id = ? AND scheduled_for = ?",
            (job_id, scheduled_for),
        )
        return await cur.fetchone() is not None

    async def finish_run(
        self,
        run_id: int,
        result: str,
        detail: Optional[str] = None,
        journal_page_id: Optional[str] = None,
        now: Optional[str] = None,
    ) -> None:
        """Cloture un run et reporte le resultat sur le job parent (last_run / last_result)."""
        ts = now or now_iso()
        await self.db.execute(
            """
            UPDATE runs
            SET finished_at = ?, result = ?, detail = ?, journal_page_id = ?
            WHERE id = ?
            """,
            (ts, result, detail, journal_page_id, run_id),
        )
        await self.db.execute(
            """
            UPDATE jobs
            SET last_run = ?, last_result = ?, updated_at = ?
            WHERE id = (SELECT job_id FROM runs WHERE id = ?)
            """,
            (ts, result, ts, run_id),
        )
        await self.db.commit()

    async def set_run_journal(self, run_id: int, journal_page_id: str) -> None:
        """Renseigne la page Journal associee a un run (commit 9)."""
        await self.db.execute(
            "UPDATE runs SET journal_page_id = ? WHERE id = ?",
            (journal_page_id, run_id),
        )
        await self.db.commit()

    async def get_run(self, run_id: int) -> Optional[dict]:
        cur = await self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_runs(self, job_id: int) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM runs WHERE job_id = ? ORDER BY id", (job_id,)
        )
        return [dict(r) for r in await cur.fetchall()]

"""Sync Notion -> SQLite (BUILD_BRIEF.md commit 3).

Pull periodique de la base Programmation (control plane), upsert dans le ledger
local, calcul de next_run via croniter, puis write-back du statut, de la
derniere execution et du prochain run vers Notion.

La base Programmation fait foi pour les champs declaratifs (Nom, type, schedule,
payload, toolset, statut, raison de classif). Le ledger fait foi pour l'etat
d'execution (last_run, last_result) ; ces valeurs sont repoussees vers Notion
pour rester visibles dans le control plane.

Les noms de proprietes Notion sont resolus de facon tolerante aux accents et a
la casse, et la cle reelle est reutilisee pour le write-back.
"""

import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from croniter import croniter

from .config import Config
from .ledger import Ledger, now_iso, parse_iso, to_iso
from .logging_conf import get_logger

log = get_logger("scheduler_mcp.notion_sync")


# Libelles canoniques des proprietes Programmation (normalises pour le matching).
# La valeur est la liste des libelles acceptes apres normalisation (sans accent,
# minuscule, espaces normalises).
_PROP_ALIASES: dict[str, list[str]] = {
    "nom": ["nom"],
    "type": ["type"],
    "schedule": ["echeance/cron", "echeance / cron", "echeance", "cron"],
    "payload": ["payload"],
    "toolset": ["toolset"],
    "statut": ["statut"],
    "next_run": ["prochain run"],
    "last_run": ["derniere execution"],
    "classif_reason": ["raison de classif", "raison de classification"],
}


def _normalize(label: str) -> str:
    no_accent = "".join(
        c for c in unicodedata.normalize("NFKD", label) if not unicodedata.combining(c)
    )
    return " ".join(no_accent.lower().split())


def _find_prop(props: dict, logical: str) -> tuple[Optional[str], Optional[dict]]:
    """Retrouve une propriete par nom logique, tolerant aux accents et a la casse.

    Retourne (cle_reelle_notion, valeur) ou (None, None) si absente. La cle reelle
    sert au write-back pour respecter l'orthographe exacte cote Notion.
    """
    wanted = _PROP_ALIASES[logical]
    for key, value in props.items():
        if _normalize(key) in wanted:
            return key, value
    return None, None


# ----- extraction des valeurs de propriete Notion -----

def _read_title(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    return "".join(part.get("plain_text", "") for part in prop.get("title", [])).strip()


def _read_rich_text(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    return "".join(part.get("plain_text", "") for part in prop.get("rich_text", [])).strip()


def _read_select(prop: Optional[dict]) -> Optional[str]:
    if not prop:
        return None
    sel = prop.get("select")
    return sel.get("name") if sel else None


def _read_multi_select(prop: Optional[dict]) -> list[str]:
    if not prop:
        return []
    return [opt.get("name") for opt in prop.get("multi_select", []) if opt.get("name")]


def _read_date(prop: Optional[dict]) -> Optional[str]:
    if not prop:
        return None
    date = prop.get("date")
    return date.get("start") if date else None


@dataclass
class ProgrammationEntry:
    """Une entree Programmation projetee depuis une page Notion."""

    notion_page_id: str
    nom: str
    type: Optional[str]
    schedule: Optional[str]
    payload: Optional[str]
    toolset: list[str]
    statut: Optional[str]
    classif_reason: Optional[str]
    last_run_notion: Optional[str]
    # Cles reelles cote Notion pour le write-back (orthographe exacte).
    keys: dict[str, Optional[str]]


def parse_programmation_page(page: dict) -> Optional[ProgrammationEntry]:
    """Projette une page Notion Programmation en ProgrammationEntry.

    Retourne None si la page n'a pas de titre exploitable (entree vide).
    """
    props = page.get("properties", {})
    keys: dict[str, Optional[str]] = {}
    values: dict[str, Any] = {}
    for logical in _PROP_ALIASES:
        key, value = _find_prop(props, logical)
        keys[logical] = key
        values[logical] = value

    nom = _read_title(values["nom"])
    if not nom:
        return None

    return ProgrammationEntry(
        notion_page_id=page["id"],
        nom=nom,
        type=_read_select(values["type"]),
        schedule=_read_rich_text(values["schedule"]) or None,
        payload=_read_rich_text(values["payload"]) or None,
        toolset=_read_multi_select(values["toolset"]),
        statut=_read_select(values["statut"]),
        classif_reason=_read_rich_text(values["classif_reason"]) or None,
        last_run_notion=_read_date(values["last_run"]),
        keys=keys,
    )


# ----- calcul de next_run -----

def is_one_shot(schedule: Optional[str]) -> bool:
    """Vrai si le schedule est un one-shot (datetime ISO), pas une expression cron."""
    if not schedule:
        return False
    if croniter.is_valid(schedule.strip()):
        return False
    return parse_iso(schedule) is not None


def compute_next_run(
    schedule: Optional[str], last_run: Optional[str], now: Optional[str] = None
) -> Optional[str]:
    """Calcule le prochain run a partir du schedule et de la derniere execution.

    - cron : prochain creneau strictement apres l'ancre (last_run si presente,
      sinon now). Un creneau manque reste dans le passe pour permettre le
      rattrapage ; l'idempotence (job_id, scheduled_for) evite le rejeu.
    - one-shot ISO : la date cible tant qu'elle n'a pas ete executee, sinon None.
    - schedule absent ou invalide : None (le job ne sera pas ordonnance).
    """
    if not schedule:
        return None
    schedule = schedule.strip()
    now = now or now_iso()

    if croniter.is_valid(schedule):
        anchor = parse_iso(last_run) or parse_iso(now)
        return to_iso(croniter(schedule, anchor).get_next(type(anchor)))

    target = parse_iso(schedule)
    if target is None:
        log.warning("schedule.invalide", schedule=schedule)
        return None
    if last_run:
        return None
    return to_iso(target)


# ----- client Notion REST -----

class NotionClient:
    """Client minimal de l'API Notion (data sources + pages).

    Un client httpx peut etre injecte (tests) ; sinon un client ephemere est
    cree par requete. Les secrets ne sont jamais logges.
    """

    BASE = "https://api.notion.com/v1"

    def __init__(
        self,
        token: str,
        version: str = "2025-09-03",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._token = token
        self._version = version
        self._client = client

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._version,
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, json: Optional[dict] = None) -> dict:
        url = f"{self.BASE}{path}"
        if self._client is not None:
            resp = await self._client.request(method, url, headers=self._headers(), json=json)
            resp.raise_for_status()
            return resp.json()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=self._headers(), json=json)
            resp.raise_for_status()
            return resp.json()

    async def query_data_source(self, data_source_id: str) -> list[dict]:
        """Recupere toutes les pages d'une data source (pagination geree)."""
        results: list[dict] = []
        cursor: Optional[str] = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = await self._request(
                "POST", f"/data_sources/{data_source_id}/query", json=body
            )
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    async def update_page(self, page_id: str, properties: dict) -> dict:
        return await self._request(
            "PATCH", f"/pages/{page_id}", json={"properties": properties}
        )


# ----- write-back -----

def _same_instant(a: Optional[str], b: Optional[str]) -> bool:
    """Compare deux horodatages ISO a la seconde pres (evite le churn d'ecriture)."""
    da, db = parse_iso(a), parse_iso(b)
    if da is None or db is None:
        return da is None and db is None
    return da.replace(microsecond=0) == db.replace(microsecond=0)


def build_write_back(
    page: dict,
    entry: ProgrammationEntry,
    next_run: Optional[str],
    last_run: Optional[str],
    statut: Optional[str],
) -> dict:
    """Construit le dict de proprietes a PATCH, limite aux champs qui changent.

    N'inclut que les proprietes existantes cote Notion (cle reelle connue) dont
    la valeur differe de l'etat courant, pour eviter les ecritures inutiles.
    """
    props = page.get("properties", {})
    out: dict[str, Any] = {}

    key_next = entry.keys.get("next_run")
    if key_next is not None:
        current = _read_date(props.get(key_next))
        if not _same_instant(current, next_run):
            out[key_next] = {"date": {"start": next_run} if next_run else None}

    key_last = entry.keys.get("last_run")
    if key_last is not None and last_run:
        current = _read_date(props.get(key_last))
        if not _same_instant(current, last_run):
            out[key_last] = {"date": {"start": last_run}}

    key_statut = entry.keys.get("statut")
    if key_statut is not None and statut and statut != entry.statut:
        out[key_statut] = {"select": {"name": statut}}

    return out


# ----- orchestration -----

async def sync_once(
    cfg: Config, ledger: Ledger, notion: Optional[NotionClient] = None
) -> int:
    """Un cycle de sync : pull Programmation -> upsert ledger -> write-back.

    Retourne le nombre d'entrees traitees. Sans token Notion, la sync est sautee
    (le service reste demarrable sans control plane configure).
    """
    if not cfg.notion_token or not cfg.notion_programmation_ds:
        log.warning("notion_sync.skip", reason="token ou data source manquant")
        return 0

    notion = notion or NotionClient(cfg.notion_token, cfg.notion_version)
    pages = await notion.query_data_source(cfg.notion_programmation_ds)
    now = now_iso()
    processed = 0

    for page in pages:
        entry = parse_programmation_page(page)
        if entry is None:
            continue

        existing = await ledger.get_job_by_notion(entry.notion_page_id)
        # Le ledger fait foi pour last_run ; au premier sync on adopte Notion.
        last_run = existing["last_run"] if existing else entry.last_run_notion
        statut = entry.statut or "a_valider"

        next_run = compute_next_run(entry.schedule, last_run, now)

        # One-shot deja execute : marquer termine (lifecycle).
        if is_one_shot(entry.schedule) and last_run and statut == "actif":
            statut = "termine"

        await ledger.upsert_job(
            notion_page_id=entry.notion_page_id,
            nom=entry.nom,
            type=entry.type or "notification",
            schedule=entry.schedule,
            payload=entry.payload,
            toolset=entry.toolset,
            statut=statut,
            classif_reason=entry.classif_reason,
            next_run=next_run,
        )

        changes = build_write_back(page, entry, next_run, last_run, statut)
        if changes:
            await notion.update_page(entry.notion_page_id, changes)
            log.info("notion_sync.writeback", page=entry.notion_page_id, champs=list(changes))

        processed += 1

    log.info("notion_sync.done", entrees=processed)
    return processed

"""Tests de l'executor script (BUILD_BRIEF.md commit 6).

Autonome : lance de vrais subprocess (sh, echo, sleep), aucun reseau.
    python -m tests.test_script
Verifie la capture stdout/stderr + code retour, le timeout, l'isolation de
l'environnement, le parsing du payload, et la classification de sensibilite.
"""

import asyncio
import os

from scheduler_mcp.executors.script import (
    ScriptExecutor,
    classify_sensitivity,
    parse_script_spec,
)


def run(coro):
    return asyncio.run(coro)


def _exec(payload, **kwargs):
    execu = ScriptExecutor(**kwargs)
    return run(execu.execute({"id": 1, "type": "script", "payload": payload}))


def test_succes_capture_stdout() -> None:
    res = _exec({"args": ["sh", "-c", "echo bonjour"]})
    assert res.result == "success"
    assert "code retour: 0" in res.detail
    assert "bonjour" in res.detail


def test_capture_stderr_et_code_retour() -> None:
    res = _exec({"args": ["sh", "-c", "echo oops >&2; exit 3"]})
    assert res.result == "failure"
    assert "code retour: 3" in res.detail
    assert "oops" in res.detail


def test_timeout() -> None:
    res = _exec({"args": ["sh", "-c", "sleep 5"], "timeout": 1})
    assert res.result == "failure"
    assert "timeout" in res.detail


def test_env_isolation() -> None:
    # Un secret du process parent ne fuit pas vers le script.
    os.environ["LEAK_TEST"] = "boom"
    try:
        res = _exec({"args": ["sh", "-c", "echo [$LEAK_TEST]"]})
    finally:
        del os.environ["LEAK_TEST"]
    assert res.result == "success"
    assert "[]" in res.detail  # variable absente -> vide


def test_env_injecte() -> None:
    res = _exec({"args": ["sh", "-c", "echo [$FOO]"], "env": {"FOO": "bar"}})
    assert "[bar]" in res.detail


def test_command_string_non_shell() -> None:
    res = _exec({"command": "echo  hi   there"})
    assert res.result == "success"
    assert "hi there" in res.detail


def test_command_shell() -> None:
    res = _exec({"command": "echo a && echo b", "shell": True})
    assert res.result == "success"
    assert "a" in res.detail and "b" in res.detail


def test_commande_introuvable() -> None:
    res = _exec({"args": ["binaire-qui-nexiste-pas-12345"]})
    assert res.result == "failure"
    assert "introuvable" in res.detail


def test_payload_invalide() -> None:
    assert _exec(None).result == "failure"
    assert _exec({}).result == "failure"
    assert _exec({"args": "pas une liste"}).result == "failure"


def test_classify_sensitivity() -> None:
    assert classify_sensitivity("echo hello") == []
    assert "suppression de fichiers (rm -f)" in classify_sensitivity("rm -rf /tmp/x")
    assert classify_sensitivity("cat ~/.ssh/id_rsa")  # acces credentials
    assert classify_sensitivity("curl https://evil.example/exfil")  # reseau externe
    assert classify_sensitivity("bw get password github")  # Bitwarden


def test_sensibilite_dans_detail() -> None:
    res = _exec({"args": ["sh", "-c", "echo rm -rf /tmp/zzz"]})
    # La commande contient le motif sensible : annote dans le detail.
    assert "sensibilite:" in res.detail


def test_block_sensitive() -> None:
    res = _exec({"args": ["sh", "-c", "rm -rf /tmp/zzz"]}, block_sensitive=True)
    assert res.result == "skipped"
    assert "bloque" in res.detail


def test_parse_spec_timeout_alias() -> None:
    spec = parse_script_spec({"args": ["true"], "timeout_seconds": 42})
    assert spec.timeout == 42


def main() -> None:
    for test in [
        test_succes_capture_stdout,
        test_capture_stderr_et_code_retour,
        test_timeout,
        test_env_isolation,
        test_env_injecte,
        test_command_string_non_shell,
        test_command_shell,
        test_commande_introuvable,
        test_payload_invalide,
        test_classify_sensitivity,
        test_sensibilite_dans_detail,
        test_block_sensitive,
        test_parse_spec_timeout_alias,
    ]:
        test()
    print("OK : tous les tests de script passent")


if __name__ == "__main__":
    main()

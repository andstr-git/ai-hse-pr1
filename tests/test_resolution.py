"""One test per mandatory resolution rule from ASSIGNMENT.md.

The fixture is tests/fixtures/rules_pkg; questions run through the CLI process,
so both the index and the JSON output contract are covered.
"""

import json
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "rules_pkg"


def run_query(*args: str) -> subprocess.CompletedProcess:
    process = subprocess.run(
        [sys.executable, "-m", "repo_map.cli", *args, "--root", str(FIXTURE)],
        capture_output=True,
        text=True,
    )
    return process


def query(*args: str) -> dict:
    process = run_query(*args)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def internal_callees(name: str) -> set[str]:
    result = query("callees", name)
    return {item["qualified_name"] for item in result["results"]}


def unresolved_callees(name: str) -> dict[str, str]:
    result = query("callees", name)
    return {item["expression"]: item["reason"] for item in result["unresolved"]}


def external_callees(name: str) -> dict[str, str]:
    result = query("callees", name)
    return {item["expression"]: item["target"] for item in result["external"]}


def test_same_module_name():
    assert "rules_pkg.mod._helper" in internal_callees("rules_pkg.mod.same_module")


def test_nested_function():
    assert "rules_pkg.mod.nested_outer.nested_inner" in internal_callees(
        "rules_pkg.mod.nested_outer"
    )


def test_from_import_alias_relative():
    assert "rules_pkg.sub.target" in internal_callees("rules_pkg.mod.via_alias")


def test_from_import_module_attribute():
    assert "rules_pkg.sub.target" in internal_callees("rules_pkg.mod.via_module")


def test_import_module_attribute():
    assert "rules_pkg.sub.target" in internal_callees("rules_pkg.mod.via_absolute")


def test_reexport_chain():
    result = query("definition", "rules_pkg.target")
    assert result["symbol"] == "rules_pkg.sub.target"


def test_class_call():
    callees = internal_callees("rules_pkg.mod.make")
    assert "rules_pkg.mod.Maker" in callees
    assert "rules_pkg.mod.Maker.__init__" not in callees


def test_self_method():
    assert "rules_pkg.mod.Maker.decorate" in internal_callees("rules_pkg.mod.Maker.build")


def test_shadowed_parameter_is_not_resolved():
    unresolved = unresolved_callees("rules_pkg.mod.wrapper")
    assert unresolved.get("target") is not None
    assert not internal_callees("rules_pkg.mod.wrapper")


def test_shadowed_local_is_not_resolved():
    unresolved = unresolved_callees("rules_pkg.mod.local_shadow")
    assert unresolved.get("run_target") is not None


def test_external_target():
    assert external_callees("rules_pkg.mod.external_call") == {"json.dumps": "json.dumps"}


def test_unknown_name_is_unresolved():
    unresolved = unresolved_callees("rules_pkg.mod.unknown_call")
    assert unresolved.get("mystery") is not None


def test_references_kinds():
    result = query("references", "rules_pkg.sub.target")
    by_kind = [(item["kind"], item["file"], item["line"]) for item in result["results"]]
    assert ("import", "rules_pkg/mod.py", 6) in by_kind
    assert any(kind == "call" and file == "rules_pkg/mod.py" for kind, file, _ in by_kind)


def test_unknown_query_fails_with_error():
    process = run_query("definition", "rules_pkg.nothing")
    assert process.returncode == 1
    assert "unknown symbol" in process.stderr

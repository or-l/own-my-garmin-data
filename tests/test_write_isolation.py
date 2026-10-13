"""Invariant 1: Garmin write endpoints are referenced only in garmin_write.py,
and the ingestion path never imports the write side. The endpoints are
methods on the Garmin client, not importable names, so this scans the AST
for attribute accesses / string constants, not just imports."""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WRITE_ENDPOINTS = {"upload_workout", "schedule_workout", "unschedule_workout", "delete_workout"}
ALLOWED = {"garmin_write.py"}
SKIP_DIRS = {".venv", ".claude", ".git", "tests", "probe_out"}
INGESTION = ["domains.py", "upserts.py", "config.py", "backfill.py", "ingestion/pull.py", "apply_schema.py"]
WRITE_SIDE_MODULES = {"garmin_write", "seed_workouts", "sync_schedule", "plan_admin", "coach_roles"}


def _python_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in SKIP_DIRS:
            continue
        yield rel.as_posix(), path


def _endpoint_refs(tree) -> set[str]:
    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in WRITE_ENDPOINTS:
            refs.add(node.attr)
        elif isinstance(node, ast.Constant) and node.value in WRITE_ENDPOINTS:
            refs.add(node.value)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            refs |= {a.name for a in node.names} & WRITE_ENDPOINTS
    return refs


def test_scan_actually_sees_garmin_write():
    tree = ast.parse((ROOT / "garmin_write.py").read_text())
    assert _endpoint_refs(tree) == WRITE_ENDPOINTS


def test_only_garmin_write_references_write_endpoints():
    offenders = {}
    for rel, path in _python_files():
        if rel in ALLOWED:
            continue
        refs = _endpoint_refs(ast.parse(path.read_text()))
        if refs:
            offenders[rel] = sorted(refs)
    assert offenders == {}


def test_ingestion_never_imports_write_side():
    for rel in INGESTION:
        tree = ast.parse((ROOT / rel).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & WRITE_SIDE_MODULES), f"{rel} imports {imported & WRITE_SIDE_MODULES}"

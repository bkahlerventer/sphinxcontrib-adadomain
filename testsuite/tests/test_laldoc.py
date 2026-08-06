"""
test_laldoc: pytest harness for the sphinxcontrib-adadomain testsuite.

The previous testsuite used ``e3.testsuite`` (an AdaCore framework)
whose ``e3.testsuite`` submodule is not present in e3-core 22.10.0
installed on this host. This pytest harness replaces it with the
same fixture format: each subdirectory of this directory contains
a ``pkg.ads`` (the Ada source to document), a ``test.out`` (the
expected laldoc RST output), and optionally a ``test.yaml`` (driver
selector + future options).

Drivers supported (via ``test.yaml``):

  driver: laldoc
      Default. Runs ``python3 -m laldoc.generate_rst`` against the
      fixture's ``prj/`` directory if present, else the fixture
      directory itself, and diffs the produced ``pkg.rst`` against
      ``test.out``.

Discovery rule: every immediate subdirectory of this directory is
treated as a test case. There is no ``__init__.py`` needed (pytest
auto-discovers ``test_*.py`` files).

Usage:

  # Run all tests.
  pytest testsuite/tests/test_laldoc.py -v

  # Rewrite all baselines (CI flag for maintainers).
  pytest testsuite/tests/test_laldoc.py -v --rewrite-baselines

The harness is intentionally strict: the produced ``pkg.rst`` must
match ``test.out`` byte-for-byte (modulo a final newline). Any
change in laldoc's output for a fixture is a deliberate baseline
update, never a silent regression.
"""
from __future__ import annotations

import os
import os.path as P
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Directory holding this test file. Subdirectories are the fixtures.
TESTS_DIR = Path(__file__).resolve().parent

# Project root (parent of ``testsuite/``). Used to find the local
# ``laldoc`` source tree so the harness uses fork code, not the
# system install (Pitfall #19: site-packages shadow trap).
PROJECT_ROOT = TESTS_DIR.parent
LALDOC_SRC = PROJECT_ROOT / "laldoc"


def _collect_fixtures():
    """Return ``[(fixture_name, fixture_dir), ...]`` sorted by name.

    A directory is a fixture if it contains a ``pkg.ads`` file. We
    skip directories without one (e.g. ``__pycache__``).
    """
    fixtures = []
    for child in sorted(TESTS_DIR.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(("_", ".")):
            continue
        if (child / "pkg.ads").exists():
            fixtures.append((child.name, child))
    return fixtures


def _driver_for(fixture_dir: Path) -> str:
    """Return the driver name from ``test.yaml`` or ``"laldoc"``."""
    yaml_path = fixture_dir / "test.yaml"
    if not yaml_path.exists():
        return "laldoc"
    for line in yaml_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("driver:"):
            return line.split(":", 1)[1].strip()
    return "laldoc"


def _run_laldoc(fixture_dir: Path, working_dir: Path, fixture_name: str) -> Path:
    """Run laldoc against ``fixture_dir`` and return the produced RST path.

    The fixture's ``pkg.ads`` lives at ``fixture_dir/pkg.ads`` and
    may have an optional ``prj/`` subdirectory carrying a custom
    GPR. We synthesise a GPR pointing at ``fixture_dir`` (or the
    fixture's own ``prj/`` if present) and run laldoc from a
    working dir so any side effects stay out of the source tree.
    """
    # Copy the fixture's source files into the working_dir so the
    # synthesised GPR's Source_Dirs resolves them. This is the
    # simplest portable approach: laldoc's GPR-driven walk reads
    # ``Source_Dirs`` as a non-recursive list of immediate dirs,
    # and the fixture layout is "everything at the top level".
    # We exclude ``test.yaml`` and ``test.out`` from the copy
    # because laldoc would otherwise see them as source files.
    excluded = {"test.yaml", "test.out"}
    for child in fixture_dir.iterdir():
        if child.name in excluded:
            continue
        target = working_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)

    # Synthesise a project file pointing at the working_dir
    # (which now mirrors the fixture's source layout).
    gpr_path = working_dir / "p.gpr"
    gpr_path.write_text(
        "project P is\n"
        f'   for Source_Dirs use ("{working_dir}");\n'
        "end P;\n"
    )

    # PYTHONPATH prepend the fork's laldoc so the local edits are
    # exercised regardless of the system install (Pitfall #19).
    env = dict(os.environ)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{LALDOC_SRC}{os.pathsep}{existing_pp}"
        if existing_pp else str(LALDOC_SRC)
    )

    cmd = [
        sys.executable,
        "-m",
        "laldoc.generate_rst",
        "-P",
        str(gpr_path),
        "--output-dir",
        str(working_dir),
    ]
    proc = subprocess.run(
        cmd,
        cwd=working_dir,
        env=env,
        capture_output=True,
        timeout=60,
    )
    out = proc.stdout.decode(errors="replace")
    err = proc.stderr.decode(errors="replace")
    # libadalang emits "Parsing failed:" lines on stderr for
    # malformed Ada source. Reject those before we even check
    # the return code (laldoc often still exits 0 because it
    # has partial trees to walk).
    if "Parsing failed:" in err or "Parsing failed:" in out:
        pytest.fail(
            f"laldoc reports parse failure for fixture "
            f"{fixture_name!r}:\n"
            f"--- stderr ---\n{err}\n"
            f"--- stdout ---\n{out}"
        )
    if proc.returncode != 0:
        pytest.fail(
            f"laldoc failed for fixture {fixture_name!r} (exit "
            f"{proc.returncode}):\n"
            f"--- stderr ---\n{err}\n"
            f"--- stdout ---\n{out}"
        )
    produced = working_dir / "pkg.rst"
    if not produced.exists():
        # Diagnostic: show what IS in working_dir so we can see
        # where laldoc actually wrote.
        listing = "\n".join(
            sorted(str(p.relative_to(working_dir))
                   for p in working_dir.iterdir())
        )
        pytest.fail(
            f"laldoc exited 0 but produced no pkg.rst in "
            f"{working_dir}. Listing:\n{listing}\n"
            f"--- stderr ---\n{err}\n"
            f"--- stdout ---\n{out}"
        )
    return produced


@pytest.mark.parametrize(
    "fixture_name,fixture_dir",
    _collect_fixtures(),
    ids=[name for name, _ in _collect_fixtures()],
)
def test_laldoc_fixture(
    fixture_name: str, fixture_dir: Path, tmp_path: Path, request
):
    """Assert that laldoc's output for ``fixture_dir/pkg.ads`` matches
    ``fixture_dir/test.out`` byte-for-byte.
    """
    driver = _driver_for(fixture_dir)
    if driver != "laldoc":
        pytest.skip(
            f"fixture {fixture_name!r} uses driver {driver!r}, "
            "not implemented in this pytest harness yet"
        )

    produced = _run_laldoc(fixture_dir, tmp_path, fixture_name)
    expected_path = fixture_dir / "test.out"
    expected = (
        expected_path.read_text()
        if expected_path.exists() else ""
    )

    rewrite = request.config.getoption("--rewrite-baselines", default=False)
    if rewrite:
        expected_path.write_text(produced.read_text())
        pytest.skip(
            f"baseline for {fixture_name!r} "
            f"{'rewritten' if expected else 'created'} from current output"
        )

    # ``test.out`` was authored with an ``== pkg.rst ==`` heading
    # (an e3-testsuite convention); the produced RST does not have
    # it. Strip the heading from the expected file before comparing.
    lines = expected.splitlines()
    while lines and lines[0].startswith("==") and lines[0].endswith("=="):
        lines.pop(0)
    expected = "\n".join(lines)

    actual = produced.read_text()
    # Normalise trailing whitespace: ``test.out`` was authored
    # without a final newline, the produced file has one. Strip
    # both to a common form before comparing.
    actual = actual.rstrip("\n")
    expected = expected.rstrip("\n")
    if actual != expected:
        # Surface a precise unified diff so the failure is
        # debuggable. Show context lines so the reader can
        # see the surrounding RST shape.
        a = actual.splitlines(keepends=True)
        b = expected.splitlines(keepends=True)
        from difflib import unified_diff
        diff = "".join(
            unified_diff(b, a, fromfile=f"{fixture_name}/test.out (expected)",
                         tofile=f"{fixture_name}/pkg.rst (actual)", lineterm="")
        )
        pytest.fail(
            f"laldoc output for {fixture_name!r} differs from baseline.\n"
            f"Run with --rewrite-baselines to update.\n{diff}"
        )

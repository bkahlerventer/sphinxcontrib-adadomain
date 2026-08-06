"""
conftest: pytest configuration for the testsuite.

Registers the ``--rewrite-baselines`` flag, which rewrites each
fixture's ``test.out`` from the current laldoc output. Useful
when laldoc's output shape legitimately changes (e.g. Tier 1
adds a new body field, all baselines need updating).
"""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--rewrite-baselines",
        action="store_true",
        default=False,
        help="Rewrite test.out files from current laldoc output.",
    )

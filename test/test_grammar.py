"""Parses every sample under wypoc/samples/, failing loudly on any syntax
error.

If wyrm.gram changed, regenerate the parser first:
    .venv/bin/python -m pegen wypoc/wyrm.gram -o wypoc/parser.py -q
"""
import glob
import os

import pytest

from conftest import SAMPLES_DIR
from wypoc.parse import parse

SAMPLE_PATHS = sorted(glob.glob(os.path.join(SAMPLES_DIR, "*.wy")))


@pytest.mark.parametrize(
    "path", SAMPLE_PATHS, ids=[os.path.basename(p) for p in SAMPLE_PATHS]
)
def test_sample_parses(path):
    with open(path) as f:
        src = f.read()
    tree = parse(src)
    assert tree.body is not None

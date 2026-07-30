import os

from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import eval_program

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.path.join(REPO_ROOT, "wypoc", "samples")


def sample_path(name: str) -> str:
    return os.path.join(SAMPLES_DIR, name)


def sample_source(name: str) -> str:
    with open(sample_path(name)) as f:
        return f.read()


def eval_sample(name: str, ctx: dict | None = None) -> dict:
    if ctx is None:
        ctx = {}
    eval_program(parse(sample_source(name)), ctx)
    return ctx

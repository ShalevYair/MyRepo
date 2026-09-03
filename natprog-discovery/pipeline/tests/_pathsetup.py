"""Puts pipeline/ on sys.path so tests can `from natlib import x` without
installing the package or requiring pytest (stdlib unittest only — the
target machine for the real 800 MB run is not guaranteed to have pytest;
see WORKPLAN.md open question 7)."""
import pathlib
import sys

_PIPELINE_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

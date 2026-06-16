"""Process-wide sink for render-time template line hits.

Instrumented templates call :func:`record` as they execute. At the end of the
run the collected ``{template_path: {linenos}}`` is flushed into a coverage.py
:class:`~coverage.CoverageData` via :func:`flush_into`.

The sink is module-global on purpose: the recorder is invoked from generated
template code (through ``environment.__cov_record__``) and the flush happens in
a patched ``Coverage.save``, so both sides need a single shared process-wide
instance rather than a threaded-through object.
"""

import os
from collections.abc import Iterable

from coverage import CoverageData

_collected: dict[str, set[int]] = {}


def record(filename: str, linenos: int | Iterable[int]) -> None:
    """Record one or more executed template line numbers for ``filename``."""
    path = os.path.realpath(filename)
    bucket = _collected.setdefault(path, set())
    if isinstance(linenos, int):
        bucket.add(linenos)
    else:
        bucket.update(linenos)


def collected() -> dict[str, frozenset[int]]:
    """Return a snapshot of everything collected so far."""
    return {path: frozenset(linenos) for path, linenos in _collected.items()}


def clear() -> None:
    """Drop all collected data."""
    _collected.clear()


def flush_into(data: CoverageData, *, plugin_name: str) -> None:
    """Write collected template lines into a coverage.py data object."""
    files = {path: sorted(linenos) for path, linenos in _collected.items() if linenos}
    if not files:
        return
    data.add_file_tracers(dict.fromkeys(files, plugin_name))
    data.add_lines(files)

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

# coverage.py uses negative line numbers for entering/exiting a code object;
# (-1, first_line) is the arc by which a template "begins".
_ENTRY = -1

_collected: dict[str, set[int]] = {}
_collected_arcs: dict[str, set[tuple[int, int]]] = {}
# Last line recorded per file, so consecutively executed lines can be linked
# into arcs. Branch-mode coverage derives executed *lines* from arc endpoints,
# so every executed line has to appear in at least one arc.
_last_line: dict[str, int] = {}


def record(filename: str, linenos: int | Iterable[int]) -> None:
    """Record one or more executed template line numbers for ``filename``.

    Lines are accumulated for line coverage, and each transition between
    consecutively executed lines is linked into an arc (with a ``(-1, first)``
    entry arc) so branch-mode coverage can recover the executed lines too.
    """
    path = os.path.realpath(filename)
    bucket = _collected.setdefault(path, set())
    arcs = _collected_arcs.setdefault(path, set())
    ordered = [linenos] if isinstance(linenos, int) else sorted(linenos)
    previous = _last_line.get(path)
    for line in ordered:
        bucket.add(line)
        if previous is None:
            arcs.add((_ENTRY, line))
        elif previous != line:
            arcs.add((previous, line))
        previous = line
    if previous is not None:
        _last_line[path] = previous


def record_arc(filename: str, arc: tuple[int, int]) -> None:
    """Record one executed branch arc ``(prev, next)`` for ``filename``."""
    _collected_arcs.setdefault(os.path.realpath(filename), set()).add(arc)


def collected() -> dict[str, frozenset[int]]:
    """Return a snapshot of every line collected so far."""
    return {path: frozenset(linenos) for path, linenos in _collected.items()}


def collected_arcs() -> dict[str, frozenset[tuple[int, int]]]:
    """Return a snapshot of every branch arc collected so far."""
    return {path: frozenset(arcs) for path, arcs in _collected_arcs.items()}


def clear() -> None:
    """Drop all collected data."""
    _collected.clear()
    _collected_arcs.clear()
    _last_line.clear()


def flush_into(data: CoverageData, *, plugin_name: str, branch: bool = False) -> None:
    """Write collected template coverage into ``data``.

    A coverage data file is globally either line data or arc data, never both.
    In branch mode we therefore write *arcs only* (coverage recovers the
    executed lines from the arc endpoints); otherwise we write lines.
    """
    if branch:
        measured = {path: sorted(arcs) for path, arcs in _collected_arcs.items() if arcs}
        add = data.add_arcs
    else:
        measured = {path: sorted(linenos) for path, linenos in _collected.items() if linenos}
        add = data.add_lines
    if not measured:
        return
    data.add_file_tracers(dict.fromkeys(measured, plugin_name))
    add(measured)

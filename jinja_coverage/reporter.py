"""coverage.py ``FileReporter`` for Jinja2 templates.

The set of executable lines is derived from the *same* instrumentation used at
render time (:func:`jinja_coverage.instrument.executable_lines`), so the
"executed" lines recorded during a run can never fall outside the "executable"
universe reported here.
"""

import re

from coverage.plugin import FileReporter
from coverage.types import TArc, TLineNo

from jinja_coverage import instrument


class JinjaFileReporter(FileReporter):
    """Reports executable lines and source for a single Jinja2 template."""

    def __init__(self, filename: str, exclude_regex: re.Pattern[str] | None = None) -> None:
        super().__init__(filename)
        self._exclude_regex = exclude_regex

    def _executable_lines(self) -> set[TLineNo]:
        """Every instrumentable line, before exclusions are applied."""
        return instrument.executable_lines(self.source(), filename=self.filename)

    def lines(self) -> set[TLineNo]:
        # coverage takes lines() as the statement universe and subtracts only
        # the executed set to find "missing"; excluded_lines() is informational.
        # So exclusions must be removed here, like coverage's own Python reporter.
        return self._executable_lines() - self.excluded_lines()

    def arcs(self) -> set[TArc]:
        """Possible branch arcs: ``(if-line, branch-entry)`` pairs.

        Drawn from the same instrumentation that records arcs at render time, so
        an executed arc can never fall outside this set. Arcs touching an
        excluded line are dropped, mirroring how exclusion removes lines, so an
        excluded ``{% if %}`` block adds nothing to the branch total.
        """
        possible = instrument.branch_arcs(self.source(), filename=self.filename)
        excluded = self.excluded_lines()
        return {(src, dst) for src, dst in possible if src not in excluded and dst not in excluded}

    def exit_counts(self) -> dict[TLineNo, int]:
        """Map each branch source line to its number of distinct destinations.

        coverage.py treats a line with more than one exit as a branch line, so
        every ``{% if %}`` line gets one exit per arm.
        """
        destinations: dict[TLineNo, set[TLineNo]] = {}
        for src, dst in self.arcs():
            destinations.setdefault(src, set()).add(dst)
        return {src: len(dsts) for src, dsts in destinations.items()}

    def excluded_lines(self) -> set[TLineNo]:
        """Template lines a coverage exclusion pragma removes from measurement.

        A pragma on a block tag covers the whole construct; a pragma on a
        content line covers just that line.
        """
        if self._exclude_regex is None:
            return set()
        source = self.source()
        executable = self._executable_lines()
        block_last = instrument.block_ranges(source, filename=self.filename)
        excluded: set[int] = set()
        for lineno, text in enumerate(source.splitlines(), start=1):
            if not self._exclude_regex.search(text):
                continue
            excluded.add(lineno)
            last = block_last.get(lineno)
            if last is not None:
                excluded.update(line for line in executable if lineno <= line <= last)
        return excluded & executable

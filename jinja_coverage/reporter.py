"""coverage.py ``FileReporter`` for Jinja2 templates.

The set of executable lines is derived from the *same* instrumentation used at
render time (:func:`jinja_coverage.instrument.executable_lines`), so the
"executed" lines recorded during a run can never fall outside the "executable"
universe reported here.
"""

import re

from coverage.plugin import FileReporter
from coverage.types import TLineNo

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

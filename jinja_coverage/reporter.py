"""coverage.py ``FileReporter`` for Jinja2 templates.

The set of executable lines is derived from the *same* instrumentation used at
render time (:func:`jinja_coverage.instrument.executable_lines`), so the
"executed" lines recorded during a run can never fall outside the "executable"
universe reported here.
"""

from coverage.plugin import FileReporter
from coverage.types import TLineNo

from jinja_coverage import instrument


class JinjaFileReporter(FileReporter):
    """Reports executable lines and source for a single Jinja2 template."""

    def lines(self) -> set[TLineNo]:
        return instrument.executable_lines(self.source(), filename=self.filename)

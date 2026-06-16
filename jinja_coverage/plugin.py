"""The coverage.py plugin object for Jinja2 templates.

Because we record template line hits through coverage's *data API*
(:func:`jinja_coverage.collector.flush_into`) rather than its frame tracer, the
plugin never returns a :class:`~coverage.plugin.FileTracer`. Its only live job
is to hand back a :class:`~jinja_coverage.reporter.JinjaFileReporter` at report
time, once coverage resolves a measured template back to this plugin by name.
"""

from collections.abc import Iterable
from typing import Any

from coverage.plugin import CoveragePlugin
from jinja2 import Environment

from jinja_coverage.reporter import JinjaFileReporter

_INSTALLED_FLAG = "_jinja_coverage_installed"


class JinjaCoveragePlugin(CoveragePlugin):
    """Resolves measured Jinja2 templates to their file reporters."""

    def file_tracer(self, filename: str) -> None:  # noqa: ARG002
        # Data-API plugin: we populate CoverageData directly, so there is no
        # per-file frame tracer to return.
        return None

    def file_reporter(self, filename: str) -> JinjaFileReporter:
        return JinjaFileReporter(filename)

    def sys_info(self) -> Iterable[tuple[str, Any]]:
        return [("instrumented", getattr(Environment, _INSTALLED_FLAG, False))]

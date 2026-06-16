"""The coverage.py plugin object for Jinja2 templates.

Template line hits are recorded through coverage's *data API*
(:func:`jinja_coverage.collector.flush_into`), not its frame tracer, so the
plugin never needs to act as a :class:`~coverage.plugin.FileTracer`. It registers
as a *configurer* purely to land in coverage's plugin registry, which is what
lets coverage resolve a measured template back to this plugin's
:class:`~jinja_coverage.reporter.JinjaFileReporter` at report time.

Registering as a configurer rather than a file tracer also avoids coverage's
"Plugin file tracers aren't supported with SysMonitor" warning on Python 3.14+
(where ``sys.monitoring`` is the default core); the data-API path works there
regardless, so the warning would only be noise.

The configurer role also gives us coverage's config (``configure``), from which
we read the exclusion patterns (``exclude_lines`` / ``exclude_also``) so
templates honor ``{# pragma: no cover #}``.
"""

import re
from collections.abc import Iterable
from typing import Any

from coverage.plugin import CoveragePlugin
from coverage.types import TConfigurable
from jinja2 import Environment

from jinja_coverage.reporter import JinjaFileReporter

_INSTALLED_FLAG = "_jinja_coverage_installed"
# coverage config options whose regexes mark lines excluded from measurement.
_EXCLUDE_OPTIONS = ("report:exclude_lines", "report:exclude_also")


class JinjaCoveragePlugin(CoveragePlugin):
    """Resolves measured Jinja2 templates to their file reporters."""

    def __init__(self) -> None:
        super().__init__()
        self._exclude_regex: re.Pattern[str] | None = None

    def configure(self, config: TConfigurable) -> None:
        """Capture coverage's exclusion patterns so templates honor pragmas."""
        patterns: list[str] = []
        for option in _EXCLUDE_OPTIONS:
            value = config.get_option(option)
            if isinstance(value, list):
                patterns.extend(value)
        self._exclude_regex = re.compile("|".join(f"(?:{p})" for p in patterns)) if patterns else None

    def file_reporter(self, filename: str) -> JinjaFileReporter:
        return JinjaFileReporter(filename, exclude_regex=self._exclude_regex)

    def sys_info(self) -> Iterable[tuple[str, Any]]:
        return [("instrumented", getattr(Environment, _INSTALLED_FLAG, False))]

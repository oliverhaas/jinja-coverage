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
"""

from collections.abc import Iterable
from typing import Any

from coverage.plugin import CoveragePlugin
from jinja2 import Environment

from jinja_coverage.reporter import JinjaFileReporter

_INSTALLED_FLAG = "_jinja_coverage_installed"


class JinjaCoveragePlugin(CoveragePlugin):
    """Resolves measured Jinja2 templates to their file reporters."""

    def configure(self, config: object) -> None:
        """No-op: registering as a configurer is only how we enter the registry."""

    def file_reporter(self, filename: str) -> JinjaFileReporter:
        return JinjaFileReporter(filename)

    def sys_info(self) -> Iterable[tuple[str, Any]]:
        return [("instrumented", getattr(Environment, _INSTALLED_FLAG, False))]

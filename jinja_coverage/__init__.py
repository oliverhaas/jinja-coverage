"""coverage.py plugin for measuring Jinja2 template coverage.

``coverage_init`` is the entry point coverage.py calls for every module listed
under ``[run] plugins``. It instruments Jinja2, registers the plugin so measured
templates can be resolved to a file reporter, and wraps ``Coverage.save`` so the
render-time hits collected in :mod:`jinja_coverage.collector` land in the same
``.coverage`` data file as Python coverage.
"""

from typing import Protocol

import coverage

from jinja_coverage import collector, instrument
from jinja_coverage.plugin import JinjaCoveragePlugin

__version__ = "0.1.0a1"

_plugin: JinjaCoveragePlugin | None = None
_SAVE_PATCHED_FLAG = "_jinja_coverage_patched"


class _Registry(Protocol):
    """The slice of coverage's plugin registry that ``coverage_init`` uses."""

    def add_configurer(self, plugin: object) -> None: ...


def coverage_init(reg: _Registry, options: object) -> None:  # noqa: ARG001
    """Register the Jinja2 plugin with coverage.py (the ``plugins =`` entry point)."""
    global _plugin  # noqa: PLW0603
    instrument.install()
    _patch_save()
    _plugin = JinjaCoveragePlugin()
    # Register as a configurer (not a file tracer): it lands in coverage's
    # registry so file_reporter resolves, without tripping the SysMonitor
    # file-tracer warning. See jinja_coverage.plugin for the rationale.
    reg.add_configurer(_plugin)


def _flush(cov: coverage.Coverage) -> None:
    """Write collected template hits into ``cov``'s data, if our plugin is active."""
    plugin_name = getattr(_plugin, "_coverage_plugin_name", None)
    if plugin_name is None:
        return
    branch = bool(cov.get_option("run:branch"))
    collector.flush_into(cov.get_data(), plugin_name=plugin_name, branch=branch)


def _patch_save() -> None:
    """Wrap ``Coverage.save`` to flush template hits first (idempotent)."""
    if getattr(coverage.Coverage.save, _SAVE_PATCHED_FLAG, False):
        return
    original_save = coverage.Coverage.save

    def save(self: coverage.Coverage) -> None:
        _flush(self)
        original_save(self)

    setattr(save, _SAVE_PATCHED_FLAG, True)
    # Monkeypatching coverage's save hook; the type checker can't model it.
    coverage.Coverage.save = save  # ty: ignore[invalid-assignment]

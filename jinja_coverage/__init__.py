"""coverage.py plugin for measuring Jinja2 template coverage.

``coverage_init`` is the entry point coverage.py calls for every module listed
under ``[run] plugins``. It instruments Jinja2, registers the plugin so measured
templates can be resolved to a file reporter, and wraps ``Coverage.save`` so the
render-time hits collected in :mod:`jinja_coverage.collector` land in the same
``.coverage`` data file as Python coverage.
"""

from collections.abc import Mapping
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


def coverage_init(reg: _Registry, options: Mapping[str, str]) -> None:
    """Register the Jinja2 plugin with coverage.py (the ``plugins =`` entry point).

    ``options`` is the ``[jinja_coverage]`` config section coverage passes to
    each plugin; its ``extensions`` key declares custom Jinja extensions to load
    when analyzing templates for their executable lines.
    """
    global _plugin  # noqa: PLW0603
    instrument.install()
    instrument.set_analysis_extensions(_configured_extensions(options))
    _patch_save()
    _plugin = JinjaCoveragePlugin()
    # Register as a configurer (not a file tracer): it lands in coverage's
    # registry so file_reporter resolves, without tripping the SysMonitor
    # file-tracer warning. See jinja_coverage.plugin for the rationale.
    reg.add_configurer(_plugin)


def _configured_extensions(options: Mapping[str, str]) -> list[str]:
    """Dotted import paths of custom Jinja extensions from the coverage config.

    The ``[jinja_coverage] extensions`` value is a free-form string; split it on
    commas and whitespace into individual import paths.
    """
    return options.get("extensions", "").replace(",", " ").split()


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

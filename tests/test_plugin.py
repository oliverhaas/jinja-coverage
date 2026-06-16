"""Tests for the coverage.py plugin and its ``coverage_init`` wiring."""

import os

import coverage
import pytest
from jinja2 import Environment

import jinja_coverage
from jinja_coverage import collector, instrument
from jinja_coverage.plugin import JinjaCoveragePlugin
from jinja_coverage.reporter import JinjaFileReporter


class _StubRegistry:
    """Mimics coverage's plugin registry: records tracers and names them."""

    def __init__(self):
        self.file_tracers = []
        self.configurers = []

    def add_file_tracer(self, plugin):
        # coverage sets this attribute when a plugin is registered; the flush
        # path reads it back to label the data it writes.
        plugin._coverage_plugin_name = f"jinja_coverage.{type(plugin).__name__}"
        self.file_tracers.append(plugin)

    def add_configurer(self, plugin):
        self.configurers.append(plugin)


@pytest.fixture(autouse=True)
def _isolate_global_state():
    original_save = coverage.Coverage.save
    yield
    coverage.Coverage.save = original_save
    instrument.uninstall()
    jinja_coverage._plugin = None
    collector.clear()


# -- the plugin object --------------------------------------------------------


@pytest.mark.unit
def test_file_tracer_returns_none():
    # We use the data API, not coverage's frame tracer, so no FileTracer.
    assert JinjaCoveragePlugin().file_tracer("/some/template.html") is None


@pytest.mark.unit
def test_file_reporter_returns_jinja_reporter_for_the_path():
    reporter = JinjaCoveragePlugin().file_reporter("/some/template.html")
    assert isinstance(reporter, JinjaFileReporter)
    assert reporter.filename == "/some/template.html"


@pytest.mark.unit
def test_sys_info_is_a_sequence_of_pairs():
    info = dict(JinjaCoveragePlugin().sys_info())
    assert "instrumented" in info


# -- coverage_init wiring -----------------------------------------------------


@pytest.mark.unit
def test_coverage_init_installs_instrumentation():
    jinja_coverage.coverage_init(_StubRegistry(), {})
    assert Environment.code_generator_class is instrument.InstrumentedCodeGenerator


@pytest.mark.unit
def test_coverage_init_registers_the_plugin_as_a_file_tracer():
    reg = _StubRegistry()
    jinja_coverage.coverage_init(reg, {})
    assert len(reg.file_tracers) == 1
    assert isinstance(reg.file_tracers[0], JinjaCoveragePlugin)


@pytest.mark.unit
def test_coverage_init_patches_coverage_save_once():
    jinja_coverage.coverage_init(_StubRegistry(), {})
    patched = coverage.Coverage.save
    jinja_coverage.coverage_init(_StubRegistry(), {})
    # Second init must not re-wrap an already-wrapped save.
    assert coverage.Coverage.save is patched


@pytest.mark.unit
def test_save_flushes_collected_template_lines_into_the_data(tmp_path):
    template = tmp_path / "t.html"
    template.write_text("<p>{{ x }}</p>\n")
    jinja_coverage.coverage_init(_StubRegistry(), {})
    collector.record(str(template), [1])

    data_file = tmp_path / ".coverage"
    cov = coverage.Coverage(data_file=str(data_file))
    cov.save()

    realpath = os.path.realpath(str(template))
    data = cov.get_data()
    assert realpath in data.measured_files()
    assert data.lines(realpath) == [1]
    assert data.file_tracer(realpath) == "jinja_coverage.JinjaCoveragePlugin"


@pytest.mark.unit
def test_save_without_a_registered_plugin_is_a_noop(tmp_path):
    # The save patch persists process-wide; if it ever runs without our plugin
    # registered (e.g. a plain pytest-cov save), it must do nothing.
    jinja_coverage.coverage_init(_StubRegistry(), {})
    jinja_coverage._plugin = None  # simulate "not our run"
    collector.record(str(tmp_path / "t.html"), [1])

    cov = coverage.Coverage(data_file=str(tmp_path / ".coverage"))
    cov.save()  # must not raise
    assert cov.get_data().measured_files() == set()

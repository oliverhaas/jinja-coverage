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
    """Mimics coverage's plugin registry: records plugins and names them."""

    def __init__(self):
        self.configurers = []

    def add_configurer(self, plugin):
        # coverage sets this attribute when a plugin is registered; the flush
        # path reads it back to label the data it writes.
        plugin._coverage_plugin_name = f"jinja_coverage.{type(plugin).__name__}"
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
def test_file_reporter_returns_jinja_reporter_for_the_path():
    reporter = JinjaCoveragePlugin().file_reporter("/some/template.html")
    assert isinstance(reporter, JinjaFileReporter)
    assert reporter.filename == "/some/template.html"


class _StubConfig:
    """A minimal TConfigurable: returns coverage options by name."""

    def __init__(self, **options):
        self._options = options

    def get_option(self, option_name):
        return self._options.get(option_name)

    def set_option(self, option_name, value):
        self._options[option_name] = value


@pytest.mark.unit
def test_configure_threads_exclude_patterns_into_the_file_reporter():
    plugin = JinjaCoveragePlugin()
    plugin.configure(
        _StubConfig(**{"report:exclude_lines": [r"pragma:\s*no\s*cover"], "report:exclude_also": [r"DEBUG ONLY"]}),
    )
    reporter = plugin.file_reporter("/t.html")
    assert reporter._exclude_regex is not None
    assert reporter._exclude_regex.search("x {# pragma: no cover #}")
    assert reporter._exclude_regex.search("y {# DEBUG ONLY #}")


@pytest.mark.unit
def test_configure_without_exclude_patterns_leaves_the_reporter_unfiltered():
    plugin = JinjaCoveragePlugin()
    plugin.configure(_StubConfig())
    assert plugin.file_reporter("/t.html")._exclude_regex is None


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
def test_coverage_init_registers_the_plugin_as_a_configurer():
    reg = _StubRegistry()
    jinja_coverage.coverage_init(reg, {})
    assert len(reg.configurers) == 1
    assert isinstance(reg.configurers[0], JinjaCoveragePlugin)


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
def test_save_flushes_collected_arcs_in_branch_mode(tmp_path):
    template = tmp_path / "t.html"
    template.write_text("{% if x %}\na\n{% endif %}\nb\n")
    jinja_coverage.coverage_init(_StubRegistry(), {})
    collector.record(str(template), [1, 2])
    collector.record_arc(str(template), (1, 2))

    cov = coverage.Coverage(branch=True, data_file=str(tmp_path / ".coverage"))
    cov.save()

    data = cov.get_data()
    assert data.has_arcs()
    assert (1, 2) in data.arcs(os.path.realpath(str(template)))


@pytest.mark.unit
def test_save_omits_arcs_when_not_in_branch_mode(tmp_path):
    # add_arcs would flip the data to has_arcs=True, which must not happen for a
    # line-only run (it would make coverage expect arcs from Python files too).
    template = tmp_path / "t.html"
    template.write_text("{% if x %}\na\n{% endif %}\nb\n")
    jinja_coverage.coverage_init(_StubRegistry(), {})
    collector.record(str(template), [1, 2])
    collector.record_arc(str(template), (1, 2))

    cov = coverage.Coverage(data_file=str(tmp_path / ".coverage"))  # branch off
    cov.save()

    data = cov.get_data()
    assert data.lines(os.path.realpath(str(template))) == [1, 2]
    assert not data.has_arcs()


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

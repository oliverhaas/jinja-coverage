"""Tests for the render-time line collector."""

import os

import pytest
from coverage import CoverageData

from jinja_coverage import collector


@pytest.fixture(autouse=True)
def _clear_collector():
    collector.clear()
    yield
    collector.clear()


@pytest.mark.unit
def test_record_single_lineno():
    collector.record("/tmp/a.html", 3)
    assert collector.collected() == {os.path.realpath("/tmp/a.html"): frozenset({3})}


@pytest.mark.unit
def test_record_accumulates_linenos_for_same_file():
    collector.record("/tmp/a.html", 3)
    collector.record("/tmp/a.html", 7)
    collector.record("/tmp/a.html", 3)
    assert collector.collected()[os.path.realpath("/tmp/a.html")] == frozenset({3, 7})


@pytest.mark.unit
def test_record_accepts_iterable_of_linenos():
    collector.record("/tmp/a.html", (2, 4, 6))
    assert collector.collected()[os.path.realpath("/tmp/a.html")] == frozenset({2, 4, 6})


@pytest.mark.unit
def test_record_canonicalizes_path():
    collector.record("/tmp/../tmp/a.html", 1)
    assert os.path.realpath("/tmp/a.html") in collector.collected()


@pytest.mark.unit
def test_clear_empties_collected():
    collector.record("/tmp/a.html", 1)
    collector.clear()
    assert collector.collected() == {}


@pytest.mark.unit
def test_flush_into_writes_lines_and_file_tracers(tmp_path):
    template = tmp_path / "page.html"
    template.write_text("x")
    collector.record(str(template), (1, 2))

    data = CoverageData(basename=str(tmp_path / ".coverage"))
    collector.flush_into(data, plugin_name="jinja_coverage.JinjaCoveragePlugin")

    canonical = os.path.realpath(str(template))
    assert data.lines(canonical) == [1, 2]
    assert data.file_tracer(canonical) == "jinja_coverage.JinjaCoveragePlugin"


@pytest.mark.unit
def test_flush_into_is_noop_when_nothing_collected(tmp_path):
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    collector.flush_into(data, plugin_name="whatever")
    assert data.measured_files() == set()

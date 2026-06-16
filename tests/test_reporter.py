"""Tests for the template FileReporter."""

import pytest

from jinja_coverage.reporter import JinjaFileReporter


@pytest.mark.unit
def test_lines_returns_executable_template_lines(tmp_path):
    tmpl = tmp_path / "p.html"
    tmpl.write_text("{% if x %}\n  <p>hi</p>\n{% else %}\n  <p>bye</p>\n{% endif %}\n")
    reporter = JinjaFileReporter(str(tmpl))
    assert reporter.lines() == {1, 2, 4}


@pytest.mark.unit
def test_lines_excludes_blank_and_structural_lines(tmp_path):
    tmpl = tmp_path / "p.html"
    tmpl.write_text("<p>a</p>\n\n{% for i in xs %}\n  <li>{{ i }}</li>\n{% endfor %}\n")
    reporter = JinjaFileReporter(str(tmpl))
    lines = reporter.lines()
    assert 2 not in lines  # blank
    assert 5 not in lines  # {% endfor %}
    assert {1, 3, 4} <= lines


@pytest.mark.unit
def test_source_returns_file_contents(tmp_path):
    tmpl = tmp_path / "p.html"
    content = "<p>{{ x }}</p>\n"
    tmpl.write_text(content)
    assert JinjaFileReporter(str(tmpl)).source() == content


@pytest.mark.unit
def test_source_token_lines_round_trips_source(tmp_path):
    tmpl = tmp_path / "p.html"
    tmpl.write_text("<p>{{ x }}</p>\nsecond\n")
    reporter = JinjaFileReporter(str(tmpl))
    rebuilt = "\n".join("".join(text for _, text in line) for line in reporter.source_token_lines())
    assert rebuilt == reporter.source().rstrip("\n")

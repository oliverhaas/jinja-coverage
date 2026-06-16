"""End-to-end tests: real ``coverage run`` over realistic Jinja2 projects.

These shell out to coverage.py the way a user would and assert on the combined
report, so they exercise the whole integration: plugin loading via
``coverage_init``, render-time instrumentation, the ``Coverage.save`` flush,
survival of the file-tracer mapping across ``combine``, report/HTML generation
through :class:`JinjaFileReporter`, and participation in the ``fail_under`` gate.

The templates deliberately use inheritance, includes, macros, loops and
conditionals so the asserted missing lines correspond to genuinely un-executed
template constructs rather than a toy ``if``/``else``.
"""

import json
import os
import subprocess
import sys

import pytest

# Variables our own (pytest-django / pytest-cov) test run leaks into the
# environment. A child ``coverage``/``pytest`` must not inherit them, or it
# tries to load our Django settings and subprocess-coverage hooks.
_LEAKY_VARS = frozenset(
    {
        "DJANGO_SETTINGS_MODULE",
        "PYTEST_ADDOPTS",
        "COVERAGE_PROCESS_START",
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_CORE_DATAFILE",
    },
)


def _clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _LEAKY_VARS}


_BASE_HTML = """\
<!doctype html>
<main>{% block body %}{% endblock %}</main>
"""

# Line map: 1 <li>, 2 {{ row.name }}, 3 {% if row.flagged %}, 4 <strong>flag</strong>,
# 5 {% endif %}, 6 </li>. No row is flagged, so line 4 must report missing.
_ROW_HTML = """\
<li>
{{ row.name }}
{% if row.flagged %}
<strong>flag</strong>
{% endif %}
</li>
"""

# Line map: 2/3 macro ``used`` (called -> covered), 5/6 macro ``never`` (never
# called -> body line 6 missing), 9 <h1>, 12 {% include %}, 14/15 {% else %}
# body (rows is non-empty -> line 15 missing), 17 the ``used`` call.
_PAGE_HTML = """\
{% extends "base.html" %}
{% macro used(x) %}
<em>{{ x }}</em>
{% endmacro %}
{% macro never(x) %}
<b>{{ x }}</b>
{% endmacro %}
{% block body %}
<h1>{{ title }}</h1>
{% if rows %}
{% for row in rows %}
{% include "_row.html" %}
{% endfor %}
{% else %}
<p>empty</p>
{% endif %}
{{ used("hi") }}
{% endblock %}
"""

_APP_PY = """\
from jinja2 import Environment, FileSystemLoader


def render_page(rows):
    env = Environment(loader=FileSystemLoader("templates"))
    return env.get_template("page.html").render(title="T", rows=rows)
"""

_TEST_APP_PY = """\
from app import render_page


def test_render_lists_rows():
    html = render_page([{"name": "a", "flagged": False}, {"name": "b", "flagged": False}])
    assert "a" in html
"""

_COVERAGERC = """\
[run]
plugins = jinja_coverage
parallel = true
source = .
"""


def _coverage(*args: str, cwd, expect_success: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-m", "coverage", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    if expect_success:
        assert result.returncode == 0, f"coverage {args} failed:\n{result.stdout}\n{result.stderr}"
    return result


def _report(project) -> dict:
    _coverage("json", "-o", "cov.json", cwd=project)
    return json.loads((project / "cov.json").read_text())["files"]


def _file(files: dict, suffix: str) -> dict:
    return files[next(key for key in files if key.endswith(suffix))]


@pytest.fixture(scope="module")
def measured_project(tmp_path_factory):
    project = tmp_path_factory.mktemp("e2e")
    templates = project / "templates"
    templates.mkdir()
    (templates / "base.html").write_text(_BASE_HTML)
    (templates / "_row.html").write_text(_ROW_HTML)
    (templates / "page.html").write_text(_PAGE_HTML)
    (project / "app.py").write_text(_APP_PY)
    (project / "test_app.py").write_text(_TEST_APP_PY)
    (project / ".coveragerc").write_text(_COVERAGERC)

    # Measure templates the way a real project does: through its own test suite.
    _coverage("run", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:django", cwd=project)
    _coverage("combine", cwd=project)
    return project


@pytest.mark.integration
def test_report_combines_python_and_template_coverage(measured_project):
    files = _report(measured_project)
    # One run, one report: Python modules and templates side by side.
    assert _file(files, "app.py")["missing_lines"] == []
    assert all(any(key.endswith(name) for key in files) for name in ("page.html", "_row.html", "base.html"))


@pytest.mark.integration
def test_uncalled_macro_and_untaken_branch_are_missing(measured_project):
    files = _report(measured_project)

    page = _file(files, "page.html")
    assert 6 in page["missing_lines"]  # body of the macro that is never called
    assert 15 in page["missing_lines"]  # {% else %} body; rows was non-empty
    assert {3, 9, 12, 17} <= set(page["executed_lines"])  # used macro, <h1>, include, call

    assert _file(files, "_row.html")["missing_lines"] == [4]  # <strong>flag</strong>, no row flagged
    assert _file(files, "base.html")["missing_lines"] == []  # inheritance fully covered


@pytest.mark.integration
def test_template_misses_fail_the_coverage_gate(measured_project):
    result = _coverage("report", "--fail-under=100", cwd=measured_project, expect_success=False)
    assert result.returncode != 0  # uncovered template lines break the run's gate
    assert "page.html" in result.stdout


@pytest.mark.integration
def test_html_report_highlights_a_missing_template_line(measured_project):
    _coverage("html", "-d", "htmlcov", cwd=measured_project)
    detail = next((measured_project / "htmlcov").glob("*page_html.html")).read_text()
    assert 'id="t15"' in detail  # the {% else %} body line...
    assert "mis" in detail  # ...is highlighted as missing


# -- Django Jinja2 backend, measured through the real coverage CLI ------------

_DJANGO_TEMPLATE = "<p>{% if x %}yes{% else %}\nno\n{% endif %}</p>\n"

_DJANGO_RUNNER = """\
import django
from django.conf import settings

settings.configure(
    TEMPLATES=[
        {
            "BACKEND": "django.template.backends.jinja2.Jinja2",
            "DIRS": ["templates"],
            "APP_DIRS": False,
            "OPTIONS": {},
        },
    ],
)
django.setup()

from django.template import engines

engines["jinja2"].get_template("page.html").render({"x": True})
"""


@pytest.mark.integration
def test_django_jinja2_backend_measured_through_coverage(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "page.html").write_text(_DJANGO_TEMPLATE)
    (tmp_path / "run.py").write_text(_DJANGO_RUNNER)
    (tmp_path / ".coveragerc").write_text("[run]\nplugins = jinja_coverage\n")

    run = _coverage("run", "run.py", cwd=tmp_path)
    # We register as a configurer, not a file tracer, so coverage must not emit
    # its "file tracers aren't supported with SysMonitor" warning (Python 3.14+).
    assert "SysMonitor" not in run.stderr

    page = _file(_report(tmp_path), "page.html")
    assert 1 in page["executed_lines"]  # {% if x %}yes -> taken branch
    assert 2 in page["missing_lines"]  # the else body "no" -> never rendered

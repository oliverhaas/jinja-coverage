"""End-to-end test: a real ``coverage run`` measures a Jinja2 template.

This shells out to coverage.py exactly as a user would (``coverage run`` with
``parallel = true``, then ``coverage combine``), so it exercises the full
integration: plugin loading via ``coverage_init``, render-time instrumentation,
the ``Coverage.save`` flush, survival of the file-tracer mapping across
``combine``, and report generation through :class:`JinjaFileReporter`.
"""

import json
import subprocess
import sys

import pytest

_TEMPLATE = """{% if x %}
  <p>yes</p>
{% else %}
  <p>no</p>
{% endif %}
"""

_RUNNER = """\
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader("templates"))
# Render taking only the truthy branch; the else body must show up as missing.
print(env.get_template("page.html").render(x=True))
"""

_COVERAGERC = """\
[run]
plugins = jinja_coverage
parallel = true
"""


def _run_coverage(*args: str, cwd) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-m", "coverage", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"coverage {args} failed:\n{result.stdout}\n{result.stderr}"
    return result


@pytest.fixture(scope="module")
def measured_project(tmp_path_factory):
    project = tmp_path_factory.mktemp("e2e_project")
    (project / "templates").mkdir()
    (project / "templates" / "page.html").write_text(_TEMPLATE)
    (project / "run.py").write_text(_RUNNER)
    (project / ".coveragerc").write_text(_COVERAGERC)

    _run_coverage("run", "run.py", cwd=project)
    _run_coverage("combine", cwd=project)
    return project


@pytest.mark.integration
def test_json_report_marks_the_unrendered_branch_as_missing(measured_project):
    _run_coverage("json", "-o", "coverage.json", cwd=measured_project)
    report = json.loads((measured_project / "coverage.json").read_text())

    template_key = next(key for key in report["files"] if key.endswith("page.html"))
    file_report = report["files"][template_key]
    executed = set(file_report["executed_lines"])
    missing = set(file_report["missing_lines"])

    assert {1, 2} <= executed  # {% if x %} and the taken <p>yes</p>
    assert 4 in missing  # the <p>no</p> body never rendered
    assert 2 not in missing


@pytest.mark.integration
def test_html_report_includes_the_template(measured_project):
    _run_coverage("html", "-d", "htmlcov", cwd=measured_project)
    htmlcov = measured_project / "htmlcov"

    assert "page.html" in (htmlcov / "index.html").read_text()  # linked in the index

    detail = next(htmlcov.glob("*page_html.html")).read_text()
    assert ">no</p" in detail.replace("&lt;", "<")  # the template source is shown
    assert 'id="t4"' in detail  # line 4 is present...
    assert "mis" in detail  # ...and a line is highlighted as missing

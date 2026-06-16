"""Tests for codegen-time template instrumentation."""

import os
import traceback

import pytest
from jinja2 import Environment, FileSystemLoader
from jinja2.compiler import CodeGenerator

from jinja_coverage import collector, instrument


@pytest.fixture(autouse=True)
def _reset():
    collector.clear()
    yield
    instrument.uninstall()
    collector.clear()


# -- executable line extraction (granularity model) --------------------------


@pytest.mark.unit
def test_executable_lines_marks_if_and_body_but_not_structural_tags():
    src = "{% if x %}\n  <p>hi</p>\n{% else %}\n  <p>bye</p>\n{% endif %}\n"
    lines = instrument.executable_lines(src, filename="/p.html")
    assert 1 in lines  # {% if x %}
    assert 2 in lines  # taken-branch content
    assert 4 in lines  # else-branch content
    assert 3 not in lines  # {% else %}
    assert 5 not in lines  # {% endif %}


@pytest.mark.unit
def test_executable_lines_marks_every_line_of_multiline_literal():
    src = "<div>\n  <span>a</span>\n  <span>b</span>\n</div>\n"
    lines = instrument.executable_lines(src, filename="/p.html")
    assert {1, 2, 3, 4} <= lines


@pytest.mark.unit
def test_executable_lines_excludes_blank_lines():
    src = "<p>a</p>\n\n<p>b</p>\n"
    lines = instrument.executable_lines(src, filename="/p.html")
    assert 2 not in lines
    assert {1, 3} <= lines


@pytest.mark.unit
def test_executable_lines_for_whitespace_only_template_is_empty():
    assert instrument.executable_lines("   \n  \n", filename="/p.html") == set()


@pytest.mark.unit
def test_executable_lines_survives_compiler_exit_from_double_extends():
    # A second {% extends %} makes jinja raise CompilerExit mid-codegen; our
    # blockvisit must swallow it exactly as jinja's own does, not crash.
    lines = instrument.executable_lines('{% extends "a" %}\n{% extends "b" %}\n', filename="/p.html")
    assert lines == {1, 2}


@pytest.mark.unit
def test_executable_lines_handles_attribute_access():
    # ``{{ obj.x }}`` compiles to an ``environment.getattr`` call; the lineno
    # extractor must skip such calls and report only the output line.
    assert instrument.executable_lines("<p>{{ obj.x }}</p>\n", filename="/p.html") == {1}


@pytest.mark.unit
def test_executable_lines_marks_for_body_but_not_endfor():
    src = "{% for i in xs %}\n  <li>{{ i }}</li>\n{% endfor %}\n"
    lines = instrument.executable_lines(src, filename="/p.html")
    assert 1 in lines  # {% for %}
    assert 2 in lines  # body
    assert 3 not in lines  # {% endfor %}


# -- render-time recording ----------------------------------------------------


@pytest.mark.unit
def test_render_records_only_the_executed_branch(tmp_path):
    tmpl = tmp_path / "p.html"
    tmpl.write_text("{% if x %}\n  <p>yes</p>\n{% else %}\n  <p>no</p>\n{% endif %}\n")
    instrument.install()

    env = Environment(loader=FileSystemLoader(str(tmp_path)))
    env.get_template("p.html").render(x=True)

    executed = collector.collected()[os.path.realpath(str(tmpl))]
    assert 2 in executed  # taken branch
    assert 4 not in executed  # un-taken else branch


@pytest.mark.unit
def test_compiled_template_has_untraceable_co_filename(tmp_path):
    tmpl = tmp_path / "p.html"
    tmpl.write_text("<p>{{ x }}</p>\n")
    instrument.install()

    env = Environment(loader=FileSystemLoader(str(tmp_path)))
    template = env.get_template("p.html")
    # coverage.py refuses to trace files whose name starts with "<", which is
    # how we stop it from recording the template's generated Python as garbage.
    assert template.root_render_func.__code__.co_filename.startswith("<")


@pytest.mark.unit
def test_from_string_without_filename_records_nothing_and_renders():
    instrument.install()
    env = Environment()
    assert env.from_string("<p>{{ x }}</p>").render(x=1) == "<p>1</p>"
    assert collector.collected() == {}


@pytest.mark.unit
def test_uninstall_restores_the_default_code_generator():
    instrument.install()
    assert Environment.code_generator_class is instrument.InstrumentedCodeGenerator
    instrument.uninstall()
    assert Environment.code_generator_class is CodeGenerator


@pytest.mark.unit
def test_template_runtime_error_still_references_the_template(tmp_path):
    tmpl = tmp_path / "p.html"
    tmpl.write_text("ok\n{{ boom() }}\n")
    instrument.install()

    env = Environment(loader=FileSystemLoader(str(tmp_path)))
    with pytest.raises(Exception) as exc_info:
        env.get_template("p.html").render()

    tb = "".join(traceback.format_exception(exc_info.value))
    assert "p.html" in tb

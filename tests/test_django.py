"""Django Jinja2 backend integration.

``instrument.install()`` patches ``jinja2.Environment`` at the class level, so
the environment Django builds for its ``Jinja2`` backend inherits our code
generator with no Django-specific code. These tests pin that behaviour.
"""

import os

import pytest
from django.template import engines

from jinja_coverage import collector, instrument


@pytest.fixture(autouse=True)
def _reset():
    collector.clear()
    yield
    instrument.uninstall()
    collector.clear()


@pytest.fixture
def jinja2_template_dir(tmp_path, settings):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    settings.TEMPLATES = [
        {
            "BACKEND": "django.template.backends.jinja2.Jinja2",
            "DIRS": [str(template_dir)],
            "APP_DIRS": False,
            "OPTIONS": {},
        },
    ]
    return template_dir


@pytest.mark.integration
def test_django_backend_records_only_the_rendered_branch(jinja2_template_dir):
    template_path = jinja2_template_dir / "page.html"
    template_path.write_text("{% if x %}\n<p>hi</p>\n{% else %}\n<p>bye</p>\n{% endif %}\n")
    instrument.install()

    engines["jinja2"].get_template("page.html").render({"x": True})

    executed = collector.collected()[os.path.realpath(str(template_path))]
    assert 2 in executed  # taken branch
    assert 4 not in executed  # un-taken else branch


@pytest.mark.integration
def test_django_loaded_template_uses_the_untraceable_co_filename(jinja2_template_dir):
    (jinja2_template_dir / "page.html").write_text("<p>{{ x }}</p>\n")
    instrument.install()

    template = engines["jinja2"].get_template("page.html")
    # The Jinja2 backend wraps the underlying template in ``.template``.
    co_filename = template.template.root_render_func.__code__.co_filename
    assert co_filename.startswith("<")

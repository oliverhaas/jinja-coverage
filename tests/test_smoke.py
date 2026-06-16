"""Smoke tests for the jinja-coverage scaffold."""

import pytest
from django.template import engines

import jinja_coverage


@pytest.mark.unit
def test_package_imports():
    assert jinja_coverage.__doc__


def test_django_jinja2_backend_renders():
    template = engines["jinja2"].from_string("Hello {{ name }}")
    assert template.render({"name": "world"}) == "Hello world"

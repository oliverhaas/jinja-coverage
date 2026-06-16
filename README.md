# jinja-coverage

[![PyPI version](https://img.shields.io/pypi/v/jinja-coverage.svg?style=flat)](https://pypi.org/project/jinja-coverage/)
[![Python versions](https://img.shields.io/pypi/pyversions/jinja-coverage.svg)](https://pypi.org/project/jinja-coverage/)
[![CI](https://github.com/oliverhaas/jinja-coverage/actions/workflows/ci.yml/badge.svg)](https://github.com/oliverhaas/jinja-coverage/actions/workflows/ci.yml)

A [coverage.py](https://coverage.readthedocs.io/) plugin that measures coverage of Jinja2 templates,
the Jinja2 counterpart to [django-coverage-plugin](https://github.com/nedbat/django_coverage_plugin)
(which only covers the Django Template Language).

Status: pre-alpha scaffold. See the design notes in the [ideas repo](https://github.com/oliverhaas/ideas)
for the planned approach (render-time codegen instrumentation feeding coverage.py data).

## Installation

```console
pip install jinja-coverage
```

Django integration (optional):

```console
pip install "jinja-coverage[django]"
```

## License

MIT

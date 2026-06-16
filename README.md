# jinja-coverage

[![PyPI version](https://img.shields.io/pypi/v/jinja-coverage.svg?style=flat)](https://pypi.org/project/jinja-coverage/)
[![Python versions](https://img.shields.io/pypi/pyversions/jinja-coverage.svg)](https://pypi.org/project/jinja-coverage/)
[![CI](https://github.com/oliverhaas/jinja-coverage/actions/workflows/ci.yml/badge.svg)](https://github.com/oliverhaas/jinja-coverage/actions/workflows/ci.yml)

A [coverage.py](https://coverage.readthedocs.io/) plugin that measures line coverage of Jinja2
templates and folds it into the same `.coverage` data, HTML/XML reports, and `fail_under` gate as
your Python code. It is the Jinja2 counterpart to
[django-coverage-plugin](https://github.com/nedbat/django_coverage_plugin) (which only covers the
Django Template Language).

Status: early alpha. Line coverage, branch coverage, and `pragma: no cover` exclusions all work for
standalone Jinja2 and Django's Jinja2 backend.

## Installation

```console
pip install jinja-coverage
```

Django integration (optional):

```console
pip install "jinja-coverage[django]"
```

## Usage

Enable the plugin in your coverage configuration. In `pyproject.toml`:

```toml
[tool.coverage.run]
plugins = ["jinja_coverage"]
```

Or in `.coveragerc`:

```ini
[run]
plugins = jinja_coverage
```

Then run coverage as usual. Rendered templates appear alongside your Python files in every report:

```console
coverage run -m pytest
coverage combine   # if you ran in parallel mode
coverage report
coverage html
```

Templates that were rendered during the run are measured line by line. Lines that never executed
(an untaken `{% else %}` body, a skipped loop) show up as missing, exactly like uncovered Python.

### Branch coverage

Turn on branch coverage the same way you do for Python, and template branches are measured too:

```ini
[run]
plugins = jinja_coverage
branch = true
```

A `{% if %}`/`{% elif %}`/`{% else %}` whose arms aren't all taken, and a `{% for %}` that is only
ever entered (or only ever skipped), are reported as partial branches and count against the
`fail_under` gate. As with Python, a one-armed `{% if %}` at the end of a block whose condition is
always true reports a missing "didn't exit" branch.

Branch measurement is line-based, so a few constructs are out of scope:

- A single-line conditional (`{% if x %}a{% else %}b{% endif %}` on one line) and an inline `{{ a if
  x else b }}` ternary have both arms on the same line, so there is no distinct arc to track.
- A branch that is the *sole or final* statement of a loop body folds onto the loop's back-edge and
  is not tracked separately.

These are the same limitations coverage.py itself has for one-liners; everything else (nested
conditionals, loops, branches inside macros, includes, and inheritance blocks) is measured.

### Excluding code

The standard coverage.py exclusion mechanism works in templates via a Jinja comment. Put coverage's
exclude pattern (`pragma: no cover` by default) in a `{# ... #}` comment:

```jinja
{% if debug %}{# pragma: no cover #}
  <pre>{{ state }}</pre>
{% endif %}
```

On a block tag (`{% if %}`, `{% for %}`, `{% macro %}` ...) the pragma excludes the whole block, just
as it covers an indented suite in Python; on a content line it excludes that line alone. Custom
`exclude_lines` / `exclude_also` patterns from your coverage config are honored too.

### How it works

The plugin swaps in an instrumented Jinja2 code generator that injects a coverage-recording call in
front of every executable construct, using the reliable line numbers available at compile time. This
sidesteps the incomplete post-compile line mapping in Jinja2
([pallets/jinja#408](https://github.com/pallets/jinja/issues/408)) that blocks the "map Python hits
back to template lines" technique other plugins rely on. Recorded hits are written into coverage.py's
data file through its data API, so the result combines and reports just like Python coverage. In
branch mode the recorder also links consecutively executed lines into arcs, which is what lets an
untaken `{% if %}` arm or an unskipped `{% for %}` surface as a partial branch.

### Django

No extra configuration is needed for Django's `django.template.backends.jinja2.Jinja2` backend. The
instrumentation is installed on `jinja2.Environment` itself, so the environment Django builds picks it
up automatically once the plugin is enabled.

## License

MIT

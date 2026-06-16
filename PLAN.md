# jinja-coverage: implementation plan / starting point

Implementation plan for this package. The package is scaffolded (this repo); the PoC and
implementation start here. The original idea and design notes live in the ideas repo:
https://github.com/oliverhaas/ideas/blob/main/packages/django/jinja-coverage.md

## What we're building

A coverage.py plugin that measures line (later branch) coverage of Jinja2 templates and
folds it into the same `.coverage` data, unified HTML report, and CI `fail-under` gate as Python code.
The Jinja2 equivalent of django-coverage-plugin, which is DTL-only.

## The one decision already made

Do NOT use the "compile template to Python, map collected line hits back to source" technique
(how the Mako and Django plugins work). For Jinja2 it is blocked upstream: `debug_info` /
`get_corresponding_lineno()` are incomplete, see pallets/jinja #408 (open since 2015) and the
rejected PR #674. Instead, instrument at **codegen time**, where the line numbers are reliable.

## Key technical insight (verified locally, jinja2 3.1.2 + coverage 7.13.4)

The pieces this hinges on all exist:

- `jinja2.Environment.code_generator_class` is a swappable class attribute (default
  `jinja2.compiler.CodeGenerator`). We can subclass it and inject our own emitted code.
- Jinja2 AST nodes carry a reliable `node.lineno` at parse/codegen time (the thing that is
  broken is only the *post-compile* `debug_info` back-mapping, not the AST linenos).
- coverage.py exposes `CoveragePlugin` (`file_reporter`, `file_tracer`, `find_executable_files`,
  `configure`, `sys_info`), `FileReporter` (`lines()`, `arcs()`, `excluded_lines()`, `source()`,
  `source_token_lines()`), and `CoverageData.add_lines()` + `CoverageData.add_file_tracers()`.

So the plan: a `CodeGenerator` subclass emits a recording call (carrying `node.lineno`) at the
start of each executable construct. At render time those calls populate a collector. We feed the
collector into coverage.py via the data API, and a `FileReporter` computes the full set of
executable lines by walking the template AST.

## Step 0: read these first (do not skip)

1. **jinjatest source** (github.com/SimplifyJobs/jinjatest). Closest prior art. See exactly how it
   instruments at render time and what it tracks (branches: if/elif/else, for, macro, block,
   include, ternary). Decide what to borrow vs. do differently. It is NOT a coverage.py plugin,
   that gap is our whole reason to exist.
2. **django-coverage-plugin source** (github.com/nedbat/django_coverage_plugin). The reference for
   the coverage.py *integration* side: `coverage_init`, how it hooks the template engine on
   startup, `FileTracer` / `FileReporter`, exclusion handling. DTL-only, so the instrumentation
   half does not transfer, but the plumbing does.
3. **coverage-mako-plugin** (github.com/nedbat/coverage-mako-plugin). Smallest complete file-tracer
   plugin, good for understanding the minimal plugin shape.
4. **coverage.py plugin docs** (coverage.readthedocs.io/en/latest/plugins.html and api_plugin.html).
5. **pallets/jinja #408 and PR #674**. Know the blocker so you do not accidentally rebuild it.

## Step 1: throwaway PoC (spike before building it into the package)

Answer the single riskiest unknown first: can render-time-collected Jinja2 line data be made to
show up in a normal coverage.py report and combine with Python coverage? Spike it in one scratch
file/dir, no packaging.

**Spike A: instrumentation.** Subclass `jinja2.compiler.CodeGenerator`. Override the small set of
visit/emit methods so that each executable construct emits a call like
`environment.__cov_record__(template_name, lineno)` using the node's `lineno`. Set
`env.code_generator_class = InstrumentedCodeGenerator`. Render a template with a branch not taken
and assert the collector saw exactly the executed linenos.
  - Start narrow: output nodes (`{{ ... }}`), `if`/`for` blocks, top-level statements. Expand later.
  - Watch the known hard cases from PR #674: multi-line template data, `{% endblock %}`/end tags,
    extension tags, constant-folded nodes. Note which the PoC does not yet handle.

**Spike B: integration.** Get the collected `{filename: {linenos}}` into coverage.py:
  - At end of run, `cov.get_data().add_lines({tmpl_path: set_of_executed_linenos})` and
    `add_file_tracers({tmpl_path: "<plugin_name>"})`.
  - Implement a `FileReporter` whose `lines()` parses the template and returns ALL instrumentable
    linenos (executed + not), so coverage can compute "missing".
  - Register a minimal `CoveragePlugin` + `coverage_init`; enable with `[run] plugins = jinja_coverage`.

**PoC success criterion:** `coverage run -m pytest && coverage html` produces a report where a
`.html` Jinja template appears with the un-taken branch line flagged as missing, and
`coverage combine` merges template coverage with the Python module's coverage in one report.

If Spike B's data-API path proves awkward, fall back to studying how django-coverage-plugin drives
reporting through `file_tracer` frames and evaluate that path. But try the data API first, it
decouples us from coverage's frame tracer.

## Step 2: scaffold (DONE)

Scaffolded with the `package-init` skill. Decisions made:
  - **Name:** `jinja-coverage`, import module `jinja_coverage`. Users enable via
    `[run] plugins = jinja_coverage`.
  - **Non-Django scaffold** (ty type checker). Runtime deps `coverage>=7` + `jinja2>=3.1`.
    Django wired in as the `[django]` optional extra and in the dev group, with `tests/settings.py`
    configuring `django.template.backends.jinja2.Jinja2` and a passing smoke test that renders
    through it.
  - Python `>=3.14`. CI test matrix is `["3.14"]` for now; `3.14t` (free-threaded) is a TODO until
    dep compat is verified. Release workflows (`publish.yml`, `tag.yml`) ship dormant.

## Step 3: target architecture (rough)

```
jinja_coverage/
  __init__.py          # version, coverage_init(reg, options)
  plugin.py            # CoveragePlugin: file_reporter, configure, sys_info
  instrument.py        # InstrumentedCodeGenerator + env hook install
  collector.py         # render-time (filename, lineno) sink; flush into CoverageData
  reporter.py          # FileReporter: parse template AST -> lines(), excluded_lines(), source()
  django.py            # optional: hook Django's Jinja2 backend env (add IF needed)
```

Open design question: **how to install our code generator into the user's environments.** Options:
monkeypatch `jinja2.Environment` in `coverage_init` so newly created envs pick up our
`code_generator_class` (django-coverage-plugin does the analogous engine hook), and/or document an
explicit opt-in for envs created before coverage starts. Resolve during the spike.

## Milestones

1. PoC proves instrument + integrate + combine (Step 1). **DONE**
2. Line coverage for standalone Jinja2, broad node coverage, real HTML report. **DONE**
3. Full coverage.py integration: `combine`, `fail-under`, `report`/`html`/`xml`. **DONE**
4. Django Jinja2 backend support (test harness already scaffolded in `tests/settings.py`). **DONE**
5. Branch coverage. **DONE** for `{% if %}`/`{% elif %}`/`{% else %}` and `{% for %}` iterate/skip,
   including branches nested inside macros, includes, and inheritance blocks. Out of scope (line-based
   model): single-line conditionals and `{{ a if x else b }}` ternaries (both arms share a line), and a
   branch that is the sole/final statement of a loop body (folds onto the loop back-edge).
6. Exclusion pragmas (a `{# pragma: no cover #}` equivalent), `excluded_lines()`. **DONE** (line and
   whole-block, honoring custom `exclude_lines`/`exclude_also`).
7. Docs + first release. Docs **DONE**; release pending.

## Open questions / risks

- Does codegen-time instrumentation actually handle the PR #674 hard cases (multi-line data, end
  tags, extension tags, constant folding) that the back-mapping approach could not? This is the
  whole bet, validate early in Spike A.
- Performance overhead of a record call per construct at render time. Measure; consider compiling
  it out when coverage is not active.
- Reliably hooking environments created before `coverage_init` runs (ordering).
- Line vs branch: ship line first, branch later. Confirm with user.
- Is there real demand for coverage.py-integrated Jinja2 coverage beyond jinjatest's standalone
  pytest reporting? (Impact question from the idea file.)

## Decisions still open

- Line-only first, or attempt branch coverage in v0.
- Whether to also prototype the file_tracer/frame approach as a comparison, or commit fully to the
  data-API + codegen approach.
- Enable the `3.14t` CI leg once Django/Jinja2 free-threaded compat is checked.

(Decided during scaffold: name `jinja-coverage`; non-Django scaffold with a Django test extra.)

## References

- Idea + design notes: https://github.com/oliverhaas/ideas/blob/main/packages/django/jinja-coverage.md
- jinjatest: https://github.com/SimplifyJobs/jinjatest
- django-coverage-plugin: https://github.com/nedbat/django_coverage_plugin
- coverage-mako-plugin: https://github.com/nedbat/coverage-mako-plugin
- coverage.py plugins: https://coverage.readthedocs.io/en/latest/plugins.html
- Upstream blocker: https://github.com/pallets/jinja/issues/408 , https://github.com/pallets/jinja/pull/674
- Abandoned prior plugin (do not fork): https://github.com/MrSenko/coverage-jinja-plugin

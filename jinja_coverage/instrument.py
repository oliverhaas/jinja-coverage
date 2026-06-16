"""Codegen-time instrumentation of Jinja2 templates.

Jinja2's post-compile line mapping (``debug_info`` /
``get_corresponding_lineno``) is incomplete (pallets/jinja#408), so instead of
mapping executed Python lines back to template lines we instrument at *codegen*
time, where ``node.lineno`` is reliable. :class:`InstrumentedCodeGenerator`
injects a ``environment.__cov_record__(path, linenos)`` call in front of every
executable construct; at render time those calls populate the
:mod:`~jinja_coverage.collector`.

Two class-level hooks are installed on :class:`jinja2.Environment`:

* ``code_generator_class`` is swapped for our generator, and ``__cov_record__``
  is bound so the injected calls resolve.
* ``_compile`` is wrapped to give every compiled template a ``co_filename``
  starting with ``"<"``. coverage.py refuses to trace such "files", which stops
  it from also recording the template's *generated Python* line numbers as
  bogus hits against the template path.
"""

import ast
from collections.abc import Iterable
from types import CodeType

from jinja2 import Environment, nodes
from jinja2.compiler import CodeGenerator, CompilerExit, Frame

from jinja_coverage import collector

_RECORD_FUNC = "__cov_record__"
_INSTALLED_FLAG = "_jinja_coverage_installed"
# Two positional args: ``environment.__cov_record__(filename, linenos)``.
_RECORD_ARG_COUNT = 2


def _output_linenos(node: nodes.Output) -> set[int]:
    """Template lines that produce output for an ``Output`` node.

    Each non-blank line of literal text is marked (so multi-line literals are
    covered line by line), and each runtime expression is marked at its own
    line.
    """
    linenos: set[int] = set()
    for child in node.nodes:
        if child.lineno is None:
            # Extension-synthesized nodes may omit a lineno; nothing to record.
            continue
        if isinstance(child, nodes.TemplateData):
            for offset, segment in enumerate(child.data.split("\n")):
                if segment.strip():
                    linenos.add(child.lineno + offset)
        else:
            linenos.add(child.lineno)
    return linenos


def _record_linenos(node: nodes.Node) -> list[int]:
    """The template line numbers a statement node should record when reached."""
    if isinstance(node, nodes.Output):
        return sorted(_output_linenos(node))
    lineno = getattr(node, "lineno", 0)
    return [lineno] if lineno else []


class InstrumentedCodeGenerator(CodeGenerator):
    """A ``CodeGenerator`` that emits a coverage record before each statement."""

    def blockvisit(self, nodes: Iterable[nodes.Node], frame: Frame) -> None:
        # Mirror of CodeGenerator.blockvisit (same param names as upstream),
        # injecting a record call per node. The ``nodes`` parameter shadows the
        # module of the same name, but the body never needs the module.
        try:
            self.writeline("pass")
            for node in nodes:
                self._emit_record(node)
                self.visit(node, frame)
        except CompilerExit:
            pass

    def _emit_record(self, node: nodes.Node) -> None:
        if not self.filename:
            return
        linenos = _record_linenos(node)
        if not linenos:
            return
        arg = linenos[0] if len(linenos) == 1 else tuple(linenos)
        self.writeline(f"environment.{_RECORD_FUNC}({self.filename!r}, {arg!r})")


def _linenos_from_generated(generated_source: str) -> set[int]:
    """Extract every template lineno passed to ``__cov_record__`` in codegen."""
    linenos: set[int] = set()
    for call in ast.walk(ast.parse(generated_source)):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        if call.func.attr != _RECORD_FUNC or len(call.args) != _RECORD_ARG_COUNT:
            continue
        arg = call.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
            linenos.add(arg.value)
        elif isinstance(arg, ast.Tuple):
            linenos.update(
                elt.value for elt in arg.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, int)
            )
    return linenos


def _analysis_stub(*_args: object, **_kwargs: object) -> str:
    """A no-op filter/test, registered so analysis-time codegen can't fail."""
    return ""


def _register_referenced_callables(env: Environment, parsed: nodes.Template) -> None:
    """Stub any filters/tests ``parsed`` references but the analysis env lacks.

    We only compile ``source`` to recover its instrumentable line set, using a
    bare env that has none of the app's custom filters/tests. Jinja's codegen
    aborts on an unknown filter/test name, so register no-op stubs first; they
    don't change which lines carry a record, only whether compilation succeeds.
    """
    for node in parsed.find_all(nodes.Filter):
        env.filters.setdefault(node.name, _analysis_stub)
    for node in parsed.find_all(nodes.Test):
        env.tests.setdefault(node.name, _analysis_stub)


def executable_lines(source: str, *, filename: str, name: str | None = None) -> set[int]:
    """All instrumentable template line numbers in ``source`` (executed or not)."""
    env = Environment()  # noqa: S701 - not rendering, only compiling for analysis
    env.code_generator_class = InstrumentedCodeGenerator
    _register_referenced_callables(env, env.parse(source, name=name, filename=filename))
    generated = env.compile(source, name=name, filename=filename, raw=True)
    return _linenos_from_generated(generated)


def _record(_environment: Environment, filename: str, linenos: int | Iterable[int]) -> None:
    collector.record(filename, linenos)


def _compile_with_sentinel(_environment: Environment, source: str, filename: str) -> CodeType:
    return compile(source, f"<jinja-template:{filename}>", "exec")


# Jinja2's pristine defaults, captured at import (before any install) so uninstall
# can restore them with their exact types intact.
_DEFAULT_CODE_GENERATOR_CLASS = Environment.code_generator_class
_DEFAULT_COMPILE = Environment._compile  # noqa: SLF001


def install() -> None:
    """Instrument all Jinja2 environments for coverage measurement (idempotent)."""
    if getattr(Environment, _INSTALLED_FLAG, False):
        return
    Environment.code_generator_class = InstrumentedCodeGenerator
    # Monkeypatching jinja's compile hook; the type checker can't model it.
    Environment._compile = _compile_with_sentinel  # ty: ignore[invalid-assignment]  # noqa: SLF001
    setattr(Environment, _RECORD_FUNC, _record)
    setattr(Environment, _INSTALLED_FLAG, True)


def uninstall() -> None:
    """Undo :func:`install`, restoring Jinja2's defaults (idempotent)."""
    if not getattr(Environment, _INSTALLED_FLAG, False):
        return
    Environment.code_generator_class = _DEFAULT_CODE_GENERATOR_CLASS
    Environment._compile = _DEFAULT_COMPILE  # noqa: SLF001
    if _RECORD_FUNC in Environment.__dict__:
        delattr(Environment, _RECORD_FUNC)
    delattr(Environment, _INSTALLED_FLAG)

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
_ARC_FUNC = "__cov_arc__"
_INSTALLED_FLAG = "_jinja_coverage_installed"
# Two positional args: ``environment.__cov_record__(filename, linenos)`` and
# ``environment.__cov_arc__(filename, (prev, next))``.
_RECORD_ARG_COUNT = 2
# Destination used for a branch that leaves the template (e.g. a one-armed
# ``{% if %}`` skipped at the very end). coverage.py uses negative line numbers
# for entering/exiting a code object.
_EXIT = -1


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


def _first_line(node: nodes.Node) -> int | None:
    """The first template line recorded when ``node`` executes."""
    recorded = _record_linenos(node)
    if recorded:
        return recorded[0]
    return getattr(node, "lineno", None)


def _branch_first(body: list[nodes.Node], fallback: int | None) -> int | None:
    """First executable line of a branch ``body``, or ``fallback`` if empty."""
    for node in body:
        line = _first_line(node)
        if line is not None:
            return line
    return fallback


class InstrumentedCodeGenerator(CodeGenerator):
    """A ``CodeGenerator`` that emits coverage records as templates execute.

    Two kinds of record are injected:

    * a line record before every statement (``__cov_record__``), and
    * a branch-arc record at the head of every ``{% if %}`` branch
      (``__cov_arc__``), recording which ``(if-line, branch-entry)`` transition
      was taken so branch coverage can tell which arms ran.
    """

    def visit_Template(self, node: nodes.Template, frame: Frame | None = None) -> None:  # noqa: N802 - overrides jinja's visit_Template
        # The compile entry point: initialize per-pass branch-arc state here
        # rather than in __init__, whose signature must otherwise mirror jinja's.
        # Per-node successor line (the line control reaches once the node
        # finishes), computed while visiting the enclosing block; consumed by
        # ``visit_If`` to target a one-armed if's skip arc.
        self._node_successor: dict[int, int | None] = {}
        # Stack of "successor line for the block currently being generated".
        self._successor_stack: list[int | None] = []
        super().visit_Template(node, frame)

    def blockvisit(self, nodes: Iterable[nodes.Node], frame: Frame) -> None:
        # Mirror of CodeGenerator.blockvisit (same param names as upstream),
        # injecting a record call per node. The ``nodes`` parameter shadows the
        # module of the same name, but the body never needs the module.
        node_list = list(nodes)
        block_successor = self._successor_stack[-1] if self._successor_stack else _EXIT
        try:
            self.writeline("pass")
            for index, node in enumerate(node_list):
                following = node_list[index + 1] if index + 1 < len(node_list) else None
                self._node_successor[id(node)] = _first_line(following) if following is not None else block_successor
                self._emit_record(node)
                self.visit(node, frame)
        except CompilerExit:
            pass

    def visit_If(self, node: nodes.If, frame: Frame) -> None:  # noqa: N802 - overrides jinja's visit_If
        # Reimplements CodeGenerator.visit_If so a branch-arc record can be
        # injected at the head of each arm. Every arc is sourced from the
        # ``{% if %}`` line, so that line carries one exit per arm (a branch
        # line) and an un-taken arm shows up as a missing branch.
        if_frame = frame.soft()
        successor = self._node_successor.get(id(node), _EXIT)
        self.writeline("if ", node)
        self.visit(node.test, if_frame)
        self.write(":")
        self.indent()
        self._emit_arc(node.lineno, _branch_first(node.body, successor))
        self._visit_branch_body(node.body, if_frame, successor)
        self.outdent()
        for elif_ in node.elif_:
            self.writeline("elif ", elif_)
            self.visit(elif_.test, if_frame)
            self.write(":")
            self.indent()
            self._emit_arc(node.lineno, _branch_first(elif_.body, successor))
            self._visit_branch_body(elif_.body, if_frame, successor)
            self.outdent()
        self.writeline("else:")
        self.indent()
        if node.else_:
            self._emit_arc(node.lineno, _branch_first(node.else_, successor))
            self._visit_branch_body(node.else_, if_frame, successor)
        else:
            # No ``{% else %}`` in the template: synthesize one purely to record
            # the skip arc (the false path) at render time.
            self._emit_arc(node.lineno, successor)
            self.writeline("pass")
        self.outdent()

    def _visit_branch_body(self, body: list[nodes.Node], frame: Frame, successor: int | None) -> None:
        self._successor_stack.append(successor)
        try:
            self.blockvisit(body, frame)
        finally:
            self._successor_stack.pop()

    def _emit_record(self, node: nodes.Node) -> None:
        if not self.filename:
            return
        linenos = _record_linenos(node)
        if not linenos:
            return
        arg = linenos[0] if len(linenos) == 1 else tuple(linenos)
        self.writeline(f"environment.{_RECORD_FUNC}({self.filename!r}, {arg!r})")

    def _emit_arc(self, prev: int | None, following: int | None) -> None:
        if not self.filename or prev is None or following is None or prev == following:
            return
        self.writeline(f"environment.{_ARC_FUNC}({self.filename!r}, {(prev, following)!r})")


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


def _arcs_from_generated(generated_source: str) -> set[tuple[int, int]]:
    """Extract every ``(prev, next)`` pair passed to ``__cov_arc__`` in codegen."""
    arcs: set[tuple[int, int]] = set()
    for call in ast.walk(ast.parse(generated_source)):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        if call.func.attr != _ARC_FUNC or len(call.args) != _RECORD_ARG_COUNT:
            continue
        arg = call.args[1]
        if not (isinstance(arg, ast.Tuple) and len(arg.elts) == _RECORD_ARG_COUNT):
            continue  # pragma: no cover - the generator only ever emits 2-tuples
        prev, following = arg.elts
        if (
            isinstance(prev, ast.Constant)
            and isinstance(following, ast.Constant)
            and isinstance(prev.value, int)
            and isinstance(following.value, int)
        ):
            arcs.add((prev.value, following.value))
    return arcs


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


def _parse_for_analysis(source: str, *, filename: str, name: str | None) -> tuple[Environment, nodes.Template]:
    """Parse ``source`` with a bare env set up for structural analysis only."""
    env = Environment()  # noqa: S701 - not rendering, only compiling for analysis
    env.code_generator_class = InstrumentedCodeGenerator
    parsed = env.parse(source, name=name, filename=filename)
    _register_referenced_callables(env, parsed)
    return env, parsed


def executable_lines(source: str, *, filename: str, name: str | None = None) -> set[int]:
    """All instrumentable template line numbers in ``source`` (executed or not)."""
    env, parsed = _parse_for_analysis(source, filename=filename, name=name)
    generated = env.compile(parsed, name=name, filename=filename, raw=True)
    return _linenos_from_generated(generated)


def branch_arcs(source: str, *, filename: str, name: str | None = None) -> set[tuple[int, int]]:
    """All possible branch arcs in ``source``, as ``(if-line, branch-entry)`` pairs.

    Derived from the *same* instrumentation that records arcs at render time, so
    an executed arc can never fall outside this possible set.
    """
    env, parsed = _parse_for_analysis(source, filename=filename, name=name)
    generated = env.compile(parsed, name=name, filename=filename, raw=True)
    return _arcs_from_generated(generated)


# Node types whose template line opens a construct an exclusion pragma can cover.
_BLOCK_NODES = (
    nodes.If,
    nodes.For,
    nodes.Block,
    nodes.Macro,
    nodes.FilterBlock,
    nodes.CallBlock,
    nodes.With,
    nodes.AssignBlock,
)


def block_ranges(source: str, *, filename: str, name: str | None = None) -> dict[int, int]:
    """Map each block-opening template line to the last line of that block.

    Lets an exclusion pragma on a block tag (``{% if %}``, ``{% for %}``,
    ``{% macro %}`` ...) cover the whole construct, mirroring how coverage
    treats a pragma on a Python block header.
    """
    _, parsed = _parse_for_analysis(source, filename=filename, name=name)
    ranges: dict[int, int] = {}
    for node in parsed.find_all(_BLOCK_NODES):
        if node.lineno is None:  # pragma: no cover - block nodes from parse() always have a lineno
            continue
        last = max(
            (child.lineno for child in node.find_all(nodes.Node) if child.lineno is not None),
            default=node.lineno,
        )
        if last > ranges.get(node.lineno, 0):
            ranges[node.lineno] = last
    return ranges


def _record(_environment: Environment, filename: str, linenos: int | Iterable[int]) -> None:
    collector.record(filename, linenos)


def _record_arc(_environment: Environment, filename: str, arc: tuple[int, int]) -> None:
    collector.record_arc(filename, arc)


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
    setattr(Environment, _ARC_FUNC, _record_arc)
    setattr(Environment, _INSTALLED_FLAG, True)


def uninstall() -> None:
    """Undo :func:`install`, restoring Jinja2's defaults (idempotent)."""
    if not getattr(Environment, _INSTALLED_FLAG, False):
        return
    Environment.code_generator_class = _DEFAULT_CODE_GENERATOR_CLASS
    Environment._compile = _DEFAULT_COMPILE  # noqa: SLF001
    if _RECORD_FUNC in Environment.__dict__:
        delattr(Environment, _RECORD_FUNC)
    if _ARC_FUNC in Environment.__dict__:
        delattr(Environment, _ARC_FUNC)
    delattr(Environment, _INSTALLED_FLAG)

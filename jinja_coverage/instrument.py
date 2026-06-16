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
import warnings
from collections.abc import Iterable
from types import CodeType

from jinja2 import Environment, nodes
from jinja2.bccache import BytecodeCache
from jinja2.compiler import CodeGenerator, CompilerExit, Frame
from jinja2.exceptions import TemplateSyntaxError
from jinja2.utils import import_string

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

# Standard Jinja extensions that register their own tags. The analysis env is
# never the application's own environment, so it would not otherwise know these
# tags; loading them by default lets a template using {% do %}, {% break %}/
# {% continue %}, {% trans %} or {% debug %} be analyzed instead of aborting the
# report with "Encountered unknown tag".
_DEFAULT_ANALYSIS_EXTENSIONS: tuple[str, ...] = (
    "jinja2.ext.do",
    "jinja2.ext.loopcontrols",
    "jinja2.ext.i18n",
    "jinja2.ext.debug",
)

# Suffix mixed into the bytecode-cache key while instrumentation is installed.
# Jinja keys its cache on template name + source only, not the code generator,
# so without this an instrumented run would reuse bytecode a cache was warmed
# with while coverage was inactive (which carries no record calls) and measure
# nothing - and writing instrumented bytecode under the app's own key would
# leave its production cache full of record calls that raise once uninstalled.
_CACHE_KEY_SALT = ".jinja-coverage"


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


def _branch_first(body: list[nodes.Node], fallback: int | None) -> int | None:
    """First template line ``body`` records when reached, or ``fallback`` if none.

    Non-recording nodes (e.g. the whitespace-only output emitted between two
    tags) are skipped: control flows straight through them to the first line
    that actually records, mirroring what consecutive recording sees at render
    time. Using a node's bare ``lineno`` here would mis-target an arc at a line
    that never records.
    """
    for node in body:
        recorded = _record_linenos(node)
        if recorded:
            return recorded[0]
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
                # A node's successor is the first line its following siblings
                # record (skipping transparent whitespace nodes), or the block's
                # own successor if none of them record.
                self._node_successor[id(node)] = _branch_first(node_list[index + 1 :], block_successor)
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

    def visit_For(self, node: nodes.For, frame: Frame) -> None:  # noqa: N802 - overrides jinja's visit_For
        # Jinja's visit_For is large and fragile to reimplement, so rather than
        # injecting arc records into the loop body, the loop's *executed* arcs
        # are recovered from consecutive line recording (entered -> body line,
        # zero iterations -> the skip target). Only the *possible* arcs are
        # emitted here, as unreachable code purely for branch_arcs() to extract.
        successor = self._node_successor.get(id(node), _EXIT)
        body_first = _branch_first(node.body, successor)
        # Zero iterations runs the {% else %} arm if present, else the successor.
        skip_target = _branch_first(node.else_, successor)
        self._emit_dead_arcs([(node.lineno, body_first), (node.lineno, skip_target)])
        # A nested branch in the loop body flows on to its own next sibling; only
        # for a branch at the very tail does control loop back to the body's first
        # line, so push that as the body's successor. (A branch that is the sole
        # statement of the body degenerates onto the back-edge and isn't tracked
        # separately - the inherent limit of a line-based model of a loop.)
        self._successor_stack.append(body_first)
        try:
            super().visit_For(node, frame)
        finally:
            self._successor_stack.pop()

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

    def _write_arc_call(self, prev: int, following: int) -> None:
        self.writeline(f"environment.{_ARC_FUNC}({self.filename!r}, {(prev, following)!r})")

    def _emit_arc(self, prev: int | None, following: int | None) -> None:
        if not self.filename or prev is None or following is None or prev == following:
            return
        self._write_arc_call(prev, following)

    def _emit_dead_arcs(self, arcs: list[tuple[int | None, int | None]]) -> None:
        """Emit possible branch arcs as unreachable code, for static extraction only.

        Used where injecting a real render-time arc record would be fragile
        (loops): :func:`branch_arcs` recovers these by parsing the generated
        source, while the executed arcs come from consecutive line recording.
        """
        distinct = sorted({(p, n) for p, n in arcs if p is not None and n is not None and p != n})
        if not self.filename or not distinct:
            return
        self.writeline("if 0:")
        self.indent()
        for prev, following in distinct:
            self._write_arc_call(prev, following)
        self.outdent()


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


def _ast_int(node: ast.expr) -> int | None:
    """The int value of an AST literal, handling negatives (e.g. the ``-1`` exit).

    A negative literal parses as ``UnaryOp(USub, Constant)``, not ``Constant``,
    so the exit sentinel (``-1``) needs unwrapping to be recovered.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int)
    ):
        return -node.operand.value
    return None


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
        prev, following = (_ast_int(elt) for elt in arg.elts)
        if prev is not None and following is not None:
            arcs.add((prev, following))
    return arcs


def _analysis_stub(*_args: object, **_kwargs: object) -> str:
    """A no-op filter/test, registered so analysis-time codegen can't fail."""
    return ""


_analysis_extensions: tuple[str, ...] = _DEFAULT_ANALYSIS_EXTENSIONS
# Templates already warned about as unanalyzable, so the warning fires once per
# file rather than once per analysis pass (lines/arcs/block-ranges each parse).
_unanalyzable_warned: set[str] = set()


def set_analysis_extensions(extensions: Iterable[str]) -> None:
    """Add custom Jinja extensions to load when parsing templates for analysis.

    The standard tag-registering extensions are always kept, so this only adds
    the application's own extensions on top. Each is validated by importing it;
    an unimportable entry is skipped with a warning rather than crashing the
    report later, when the analysis env would fail to construct.
    """
    global _analysis_extensions  # noqa: PLW0603
    extra: list[str] = []
    for extension in extensions:
        try:
            import_string(extension)
        except (ImportError, AttributeError) as exc:
            warnings.warn(
                f"jinja-coverage: ignoring unimportable Jinja extension {extension!r} "
                f"from the [jinja_coverage] 'extensions' option ({exc}).",
                stacklevel=2,
            )
            continue
        extra.append(extension)
    _analysis_extensions = tuple(dict.fromkeys((*_DEFAULT_ANALYSIS_EXTENSIONS, *extra)))


def get_analysis_extensions() -> tuple[str, ...]:
    """The Jinja extensions currently loaded into the analysis env."""
    return _analysis_extensions


def _reset_analysis_state() -> None:
    """Restore analysis extensions to their defaults and clear the warning dedup."""
    global _analysis_extensions  # noqa: PLW0603
    _analysis_extensions = _DEFAULT_ANALYSIS_EXTENSIONS
    _unanalyzable_warned.clear()


def _warn_unanalyzable(filename: str, error: Exception) -> None:
    """Warn once per template that analysis failed, so the report can continue."""
    if filename in _unanalyzable_warned:
        return
    _unanalyzable_warned.add(filename)
    warnings.warn(
        f"jinja-coverage could not analyze template {filename!r} ({error}); it will be "
        f"reported as having no measurable lines. If it uses a custom Jinja extension, "
        f"declare it via the 'extensions' option in the [jinja_coverage] section of your "
        f"coverage config.",
        stacklevel=3,
    )


def _register_referenced_callables(env: Environment, parsed: nodes.Template) -> None:
    """Stub any filters/tests ``parsed`` references but the analysis env lacks.

    We only compile ``source`` to recover its instrumentable line set, using an
    env that has none of the app's custom filters/tests. Jinja's codegen aborts
    on an unknown filter/test name, so register no-op stubs first; they don't
    change which lines carry a record, only whether compilation succeeds.
    """
    for node in parsed.find_all(nodes.Filter):
        env.filters.setdefault(node.name, _analysis_stub)
    for node in parsed.find_all(nodes.Test):
        env.tests.setdefault(node.name, _analysis_stub)


def _parse_for_analysis(source: str, *, filename: str, name: str | None) -> tuple[Environment, nodes.Template]:
    """Parse ``source`` with an env set up for structural analysis only.

    The env loads the standard (and any configured) Jinja extensions so their
    tags parse, but it is never rendered, only compiled to recover the line set.
    """
    env = Environment(extensions=_analysis_extensions)  # noqa: S701 - not rendering, only compiling
    env.code_generator_class = InstrumentedCodeGenerator
    parsed = env.parse(source, name=name, filename=filename)
    _register_referenced_callables(env, parsed)
    return env, parsed


def executable_lines(source: str, *, filename: str, name: str | None = None) -> set[int]:
    """All instrumentable template line numbers in ``source`` (executed or not).

    Degrades to an empty set (with a one-time warning) if the template can't be
    parsed, e.g. it uses an undeclared custom extension tag, so one unanalyzable
    template never aborts the whole coverage report.
    """
    try:
        env, parsed = _parse_for_analysis(source, filename=filename, name=name)
        generated = env.compile(parsed, name=name, filename=filename, raw=True)
    except TemplateSyntaxError as exc:
        _warn_unanalyzable(filename, exc)
        return set()
    return _linenos_from_generated(generated)


def branch_arcs(source: str, *, filename: str, name: str | None = None) -> set[tuple[int, int]]:
    """All possible branch arcs in ``source``, as ``(if-line, branch-entry)`` pairs.

    Derived from the *same* instrumentation that records arcs at render time, so
    an executed arc can never fall outside this possible set. Degrades to an
    empty set if the template can't be parsed (see :func:`executable_lines`).
    """
    try:
        env, parsed = _parse_for_analysis(source, filename=filename, name=name)
        generated = env.compile(parsed, name=name, filename=filename, raw=True)
    except TemplateSyntaxError as exc:
        _warn_unanalyzable(filename, exc)
        return set()
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
    treats a pragma on a Python block header. Degrades to an empty mapping if
    the template can't be parsed (see :func:`executable_lines`).
    """
    try:
        _, parsed = _parse_for_analysis(source, filename=filename, name=name)
    except TemplateSyntaxError as exc:
        _warn_unanalyzable(filename, exc)
        return {}
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


def _instrumented_get_cache_key(self: BytecodeCache, name: str, filename: str | None = None) -> str:
    """A salted bytecode-cache key, so instrumented bytecode gets its own slot.

    Jinja keys its cache on template name + source, not the code generator, so
    instrumented and uninstrumented bytecode would otherwise collide on one key.
    The salt keeps them in separate cache entries: the instrumented run never
    reuses a cache warmed without coverage, and the app's own cache file is
    never overwritten with record-call bytecode that would raise once removed.
    """
    return _DEFAULT_GET_CACHE_KEY(self, name, filename) + _CACHE_KEY_SALT


# Jinja2's pristine defaults, captured at import (before any install) so uninstall
# can restore them with their exact types intact.
_DEFAULT_CODE_GENERATOR_CLASS = Environment.code_generator_class
_DEFAULT_COMPILE = Environment._compile  # noqa: SLF001
_DEFAULT_GET_CACHE_KEY = BytecodeCache.get_cache_key


def install() -> None:
    """Instrument all Jinja2 environments for coverage measurement (idempotent)."""
    if getattr(Environment, _INSTALLED_FLAG, False):
        return
    Environment.code_generator_class = InstrumentedCodeGenerator
    # Monkeypatching jinja's compile hook; the type checker can't model it.
    Environment._compile = _compile_with_sentinel  # ty: ignore[invalid-assignment]  # noqa: SLF001
    BytecodeCache.get_cache_key = _instrumented_get_cache_key  # ty: ignore[invalid-assignment]
    setattr(Environment, _RECORD_FUNC, _record)
    setattr(Environment, _ARC_FUNC, _record_arc)
    setattr(Environment, _INSTALLED_FLAG, True)


def uninstall() -> None:
    """Undo :func:`install`, restoring Jinja2's defaults (idempotent)."""
    if not getattr(Environment, _INSTALLED_FLAG, False):
        return
    Environment.code_generator_class = _DEFAULT_CODE_GENERATOR_CLASS
    Environment._compile = _DEFAULT_COMPILE  # noqa: SLF001
    BytecodeCache.get_cache_key = _DEFAULT_GET_CACHE_KEY
    if _RECORD_FUNC in Environment.__dict__:
        delattr(Environment, _RECORD_FUNC)
    if _ARC_FUNC in Environment.__dict__:
        delattr(Environment, _ARC_FUNC)
    delattr(Environment, _INSTALLED_FLAG)

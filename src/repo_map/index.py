"""Repository index: symbols, imports, call sites, references, call graph.

The module naming convention is fixed by the assignment:

    --root ../fastapi/fastapi
    fastapi/__init__.py              -> fastapi
    fastapi/applications.py          -> fastapi.applications
    fastapi/openapi/utils.py         -> fastapi.openapi.utils

The index is built in two phases:

1. a single traversal per file collects symbols, imports, call sites, shadowed
   local names and puts every call into the lexical scope it belongs to;
2. name resolution runs over the whole package: each call is resolved against
   the symbol and module tables, producing internal / external / unresolved.

Resolution follows only the rules the assignment marks as mandatory. Anything
that would require type inference, inheritance dispatch, dynamic imports, `*`
imports or data flow stays unresolved on purpose: a false edge is worse than an
honest "unresolved".
"""

import builtins
from collections.abc import Iterator
from pathlib import Path

from repo_map.graph import CallGraph
from repo_map.model import CallSite, Import, Reference, Symbol
from repo_map.parser import line_of, node_text, parse_file

BUILTINS = frozenset(dir(builtins))


def leading_dots(module_str: str) -> int:
    return len(module_str) - len(module_str.lstrip("."))


def iter_python_files(root: Path) -> Iterator[Path]:
    """All .py files under root, skipping hidden directories and __pycache__."""
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        yield path


def module_name(root: Path, path: Path) -> str:
    parts = [root.name, *path.relative_to(root).with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def relative_file(root: Path, path: Path) -> str:
    """The `file` field of the output: relative to the parent of root, with forward slashes."""
    return path.relative_to(root.parent).as_posix()


class Scope:
    """A lexical scope: a module, class, function or method body."""

    __slots__ = ("qname", "kind", "parent", "defs", "imports", "shadows", "params")

    def __init__(self, qname: str, kind: str, parent: "Scope | None"):
        self.qname = qname
        self.kind = kind  # "module" | "class" | "function" | "method"
        self.parent = parent
        self.defs: dict[str, str] = {}  # a nested def/class name -> qualified name
        self.imports: dict[str, Import] = {}  # a local import binding
        self.shadows: dict[str, str] = {}  # a local name that is not a def -> reason
        self.params: set[str] = set()


def name_chain(scope: Scope) -> list[Scope]:
    """Scopes visible for a plain-name lookup, innermost first.

    A function or method body sees module globals but not class bodies; a class
    body sees module globals only; a module sees itself.
    """
    scopes: list[Scope] = []
    current: Scope | None = scope
    while current is not None:
        scopes.append(current)
        current = current.parent
    innermost = scopes[0]
    module = scopes[-1]
    if innermost.kind == "class":
        return [innermost, module] if len(scopes) > 1 else [innermost]
    if innermost.kind in ("function", "method"):
        return [s for s in scopes if s.kind in ("function", "method", "module")]
    return [innermost]


def lookup_name(name: str, scopes: list[Scope]) -> tuple[str, object]:
    """Binding of a bare name inside the given scope chain.

    Returns one of:
        ("def", qualified_name)     a def/class from the visible scopes
        ("import", Import)          a local import binding
        ("shadow", reason)          a parameter or local variable
        ("self", class_qname)       the implicit `self` inside a method
        ("builtin", target)         a builtins.* name
        ("none", None)              unknown
    """
    for scope in name_chain(scopes[0]):
        if name in scope.defs:
            return ("def", scope.defs[name])
        if name in scope.imports:
            return ("import", scope.imports[name])
        if name in scope.shadows:
            if name == "self" and scope.kind == "method":
                return ("self", scope.qname.rpartition(".")[0])
            return ("shadow", scope.shadows[name])
    innermost = scopes[0]
    if name == "self" and innermost.kind == "method":
        return ("self", innermost.qname.rpartition(".")[0])
    if name in BUILTINS:
        return ("builtin", f"builtins.{name}")
    return ("none", None)


class RawCall:
    """A call or candidate reference found in the sources; resolved once the
    whole package is indexed."""

    __slots__ = ("expression", "file", "line", "scope")

    def __init__(self, expression: str, file: str, line: int, scope: Scope):
        self.expression = expression
        self.file = file
        self.line = line
        self.scope = scope


def _target_identifiers(node) -> list[str]:
    """Identifiers bound by an assignment/loop target (no calls or attributes)."""
    kind = node.type
    if kind == "identifier":
        return [node_text(node)]
    if kind in ("tuple", "tuple_pattern", "list_pattern", "pattern_list", "pattern"):
        names: list[str] = []
        for child in node.named_children:
            names.extend(_target_identifiers(child))
        return names
    return []


class FileIndex:
    """Everything collected from a single source file."""

    def __init__(self, root: Path, path: Path):
        self.root = root
        self.path = path
        self.rel_file = relative_file(root, path)
        self.module = module_name(root, path)
        self.is_init = path.name == "__init__.py"
        module_symbol = Symbol(self.module, "module", self.rel_file, 1, False)
        self.symbols: list[Symbol] = [module_symbol]
        self.defs: dict[str, list[Symbol]] = {self.module: [module_symbol]}
        self.module_scope = Scope(self.module, "module", None)
        self.calls: list[RawCall] = []
        self.others: list[RawCall] = []  # candidate base-class / decorator references
        self.imports: list[Import] = []
        self.call_sites: list[CallSite] = []  # filled once the package is indexed
        self.other_sites: list[CallSite] = []
        self._walk(parse_file(path).root_node, [self.module_scope])

    # --- traversal --------------------------------------------------------
    def _walk(self, node, scopes: list[Scope], parent=None) -> None:
        kind = node.type
        if kind == "decorated_definition":
            for child in node.named_children:
                if child.type in ("function_definition", "class_definition"):
                    self._define(child, scopes, parent)
                else:  # decorators are evaluated in the enclosing scope
                    self._walk(child, scopes, parent)
            return
        if kind == "decorator":
            applied = node.named_children[-1] if node.named_children else None
            if applied is not None and applied.type in ("identifier", "attribute"):
                self.others.append(
                    RawCall(node_text(applied), self.rel_file, line_of(node), scopes[-1])
                )
            return
        if kind in ("function_definition", "class_definition"):
            self._define(node, scopes, parent)
            return
        if kind == "import_statement":
            for child in node.named_children:
                if child.type in ("dotted_name", "aliased_import"):
                    self._record_module_import(child, scopes)
            return
        if kind == "import_from_statement":
            self._record_from_import(node, scopes)
            return
        if kind == "call":
            function = node.child_by_field_name("function")
            self.calls.append(
                RawCall(node_text(function), self.rel_file, line_of(node), scopes[-1])
            )
            self._walk_children(node, scopes, parent)
            return
        self._collect_bindings(node, scopes)
        self._walk_children(node, scopes, parent)

    def _walk_children(self, node, scopes: list[Scope], parent=None) -> None:
        for child in node.named_children:
            self._walk(child, scopes, parent)

    def _define(self, node, scopes: list[Scope], parent) -> None:
        outer = scopes[-1]
        name = node_text(node.child_by_field_name("name"))
        qname = f"{outer.qname}.{name}"
        is_class = node.type == "class_definition"
        if is_class:
            kind = "class"
            is_async = False
        else:
            kind = "method" if outer.kind == "class" else "function"
            is_async = any(child.type == "async" for child in node.children[:3])

        symbol = Symbol(qname, kind, self.rel_file, line_of(node), is_async)
        self.symbols.append(symbol)
        self.defs.setdefault(qname, []).append(symbol)
        outer.defs.setdefault(name, qname)

        # default values and annotations are evaluated in the enclosing scope,
        # and base classes are evaluated in the class's enclosing scope
        for field in ("parameters", "return_type", "superclasses"):
            child = node.child_by_field_name(field)
            if child is not None:
                if field == "superclasses":
                    self._record_superclasses(child, scopes)
                else:
                    self._walk_children(child, scopes, parent)

        scope = Scope(qname, kind, outer)
        if not is_class:
            scope.params = self._param_names(node.child_by_field_name("parameters"))
            scope.shadows = {param: "dynamic receiver" for param in scope.params}
        scopes.append(scope)
        self._walk_children(node, scopes, parent)
        scopes.pop()

    @staticmethod
    def _param_names(parameters) -> set[str]:
        names: set[str] = set()
        if parameters is None:
            return names
        for child in parameters.named_children:
            if child.type == "identifier":
                names.add(node_text(child))
            elif child.type == "default_parameter":
                name = child.child_by_field_name("name")
                if name is not None:
                    names.add(node_text(name))
            elif child.type == "typed_parameter":
                for sub in child.named_children:
                    if sub.type == "default_parameter":
                        name = sub.child_by_field_name("name")
                        if name is not None:
                            names.add(node_text(name))
                    elif sub.type == "identifier":
                        names.add(node_text(sub))
            elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
                for sub in child.named_children:
                    if sub.type == "identifier":
                        names.add(node_text(sub))
        return names

    def _record_superclasses(self, node, scopes: list[Scope]) -> None:
        scope = scopes[-1]
        candidates = node.named_children if node.type == "argument_list" else [node]
        for child in candidates:
            if child.type in ("identifier", "attribute"):
                self.others.append(RawCall(node_text(child), self.rel_file, line_of(child), scope))

    def _collect_bindings(self, node, scopes: list[Scope]) -> None:
        kind = node.type
        if kind in (
            "assignment",
            "augmented_assignment",
            "named_expression",
            "for_statement",
            "for_in_clause",
        ):
            left = node.child_by_field_name("left") or node.child_by_field_name("name")
        elif kind in ("with_item", "except_clause"):
            left = node.child_by_field_name("alias") or node.child_by_field_name("name")
        else:
            return
        if left is None:
            return
        for ident in _target_identifiers(left):
            scopes[-1].shadows.setdefault(ident, "local variable")

    # --- imports ----------------------------------------------------------
    def _record_module_import(self, child, scopes: list[Scope]) -> None:
        if child.type == "dotted_name":
            module, alias = node_text(child), node_text(child).split(".", 1)[0]
        else:  # aliased_import
            module = node_text(child.child_by_field_name("name"))
            alias = node_text(child.child_by_field_name("alias"))
        imp = Import(module, None, alias, self.rel_file, line_of(child), scopes[-1].qname)
        self.imports.append(imp)
        scopes[-1].imports[alias] = imp

    def _record_from_import(self, node, scopes: list[Scope]) -> None:
        module_node = node.child_by_field_name("module_name")
        module_str = node_text(module_node) if module_node is not None else "."
        for child in node.named_children:
            if child.type == "dotted_name":
                name = node_text(child)
                alias = name.split(".", 1)[0]
                imp = Import(
                    module_str, name, alias, self.rel_file, line_of(child), scopes[-1].qname
                )
            elif child.type == "aliased_import":
                name = node_text(child.child_by_field_name("name"))
                alias = node_text(child.child_by_field_name("alias"))
                imp = Import(
                    module_str, name, alias, self.rel_file, line_of(child), scopes[-1].qname
                )
            elif child.type == "wildcard_import":
                imp = Import(
                    module_str,
                    "*",
                    node_text(child) or "*",
                    self.rel_file,
                    line_of(child),
                    scopes[-1].qname,
                )
            else:
                continue
            self.imports.append(imp)
            scopes[-1].imports[alias] = imp


class RepoIndex:
    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, FileIndex] = {}
        self.defs: dict[str, list[Symbol]] = {}
        self.modules: dict[str, FileIndex] = {}
        self.graph = None

        for path in iter_python_files(root):
            fi = FileIndex(root, path)
            self.files[fi.rel_file] = fi
            self.modules[fi.module] = fi

        for fi in self.files.values():
            for symbol in fi.symbols:
                self.defs.setdefault(symbol.qualified_name, []).append(symbol)

        self._resolve_all()
        self.graph = CallGraph.from_call_sites(self.all_call_sites())

    @classmethod
    def build(cls, root: Path) -> "RepoIndex":
        return cls(root)

    def all_call_sites(self) -> list[CallSite]:
        return [site for fi in self.files.values() for site in fi.call_sites]

    def all_other_sites(self) -> list[CallSite]:
        return [site for fi in self.files.values() for site in fi.other_sites]

    # ------------------------------------------------------------------
    # Name resolution
    # ------------------------------------------------------------------
    def _import_module_target(self, imp: Import, fi: FileIndex) -> str | None:
        """Project module targeted by an import statement, None if outside the package."""
        value = imp.module
        dots = leading_dots(value)
        rest = value.lstrip(".")
        if dots:
            base = fi.module if fi.is_init else fi.module.rpartition(".")[0]
            if not base:
                return None
            target_parts = base.split(".")
            drop = dots - 1
            if drop >= len(target_parts):
                return None
            if drop:
                target_parts = target_parts[: len(target_parts) - drop]
            target_parts += rest.split(".") if rest else []
        else:
            target_parts = rest.split(".")
        target = ".".join(target_parts)
        return target if target in self.modules else None

    def _walk_symbol(self, qname: str, parts: list[str], visited: set[str]) -> object:
        """Follow an attribute chain from a symbol.

        Returns a symbol qualified name, ("module", path), or None.
        """
        if not parts:
            return qname
        if qname in visited:
            return None
        visited.add(qname)
        entry = self.defs.get(qname)
        if entry is None or not any(s.kind == "class" for s in entry):
            return None  # only class attributes are followable
        sub = f"{qname}.{parts[0]}"
        if sub in self.defs:
            return self._walk_symbol(sub, parts[1:], visited)
        return None

    def _walk_module(self, mpath: str, parts: list[str], visited: set[str]) -> object:
        """Follow an attribute chain from a project module."""
        if not parts:
            return ("module", mpath)
        if mpath in visited:
            return None
        visited.add(mpath)
        part, rest = parts[0], parts[1:]
        sub = f"{mpath}.{part}"
        if sub in self.modules:
            return self._walk_module(sub, rest, visited)
        if sub in self.defs:
            return self._walk_symbol(sub, rest, visited)
        imp = self.modules[mpath].module_scope.imports.get(part)
        if imp is not None:
            anchor = self._anchor_of_import(imp, self.modules[mpath])
            if anchor is None:
                return None
            anchor_kind, anchor_value = anchor
            if anchor_kind == "external_module":
                return ("external", None)
            if anchor_kind == "module":
                if imp.name is None:  # module-import binding: walk the remaining attrs
                    return self._walk_module(anchor_value, rest, visited)
                # from-import binding: the anchor is the imported name's module
                anchor_parts = [*imp.name.split("."), *rest]
                return self._walk_module(anchor_value, anchor_parts, visited)
        return None

    def _anchor_of_import(self, imp: Import, fi: FileIndex) -> tuple[str, object] | None:
        """Resolve an Import record to a walk anchor.

        ("module", path) | ("external_module", None) | None (unknown)
        """
        if imp.name is None:  # `import module [as alias]`
            first = imp.module.lstrip(".").split(".")[0]
            if first in self.modules:
                return ("module", first)
            return ("external_module", None)
        if imp.name == "*":  # `from module import *` is not supported on purpose
            return None
        target = self._import_module_target(imp, fi)
        if target is not None:
            return ("module", target)
        # outside the package; a plain absolute import may still be external
        if leading_dots(imp.module) == 0 and imp.module.split(".")[0] not in self.modules:
            return ("external_module", None)
        return None

    def _resolve_expression(
        self, expression: str, scopes: list[Scope], fi: FileIndex, line: int
    ) -> CallSite:
        scope = scopes[0]  # `name_chain` is innermost-first; the innermost owns the call

        def site(resolution: str, target: str | None = None, reason: str | None = None) -> CallSite:
            return CallSite(expression, fi.rel_file, line, scope.qname, resolution, target, reason)

        head, _, rest = expression.partition(".")
        parts = [head] + (rest.split(".") if rest else [])
        kind, value = lookup_name(head, scopes)

        if kind == "def":
            if not rest:
                return site("internal", value)
            walked = self._walk_symbol(value, parts[1:], set())
            if isinstance(walked, str):
                return site("internal", walked)
            if isinstance(walked, tuple) and walked[0] == "module":
                return site("internal", self.modules[walked[1]].module)
            return site("unresolved", reason="attribute not resolved")

        if kind == "import":
            anchor = self._anchor_of_import(value, fi)
            if anchor is None:
                return site("unresolved", reason="unknown name")
            anchor_kind, anchor_value = anchor
            if anchor_kind == "external_module":
                return site("external", expression)
            # a from-import first consumes the imported name, then the expression
            walk_parts = [*value.name.split("."), *parts[1:]] if value.name else parts[1:]
            walked = self._walk_module(anchor_value, walk_parts, set())
            if isinstance(walked, str):
                return site("internal", walked)
            if isinstance(walked, tuple) and walked[0] == "module":
                return site("internal", self.modules[walked[1]].module)
            return site("unresolved", reason="attribute not resolved")

        if kind == "shadow":
            return site("unresolved", reason=value)

        if kind == "self":
            if not rest:
                return site("unresolved", reason="dynamic receiver")
            walked = self._walk_symbol(value, parts[1:], set())
            if isinstance(walked, str):
                return site("internal", walked)
            return site("unresolved", reason="not defined in class")

        if kind == "builtin":
            if rest:
                return site("unresolved", reason="dynamic receiver")
            return site("external", value)

        return site("unresolved", reason="unknown name")

    def _resolve_all(self) -> None:
        for fi in self.files.values():
            for raw in fi.calls:
                site = self._resolve_expression(raw.expression, name_chain(raw.scope), fi, raw.line)
                fi.call_sites.append(site)
            for raw in fi.others:
                site = self._resolve_expression(raw.expression, name_chain(raw.scope), fi, raw.line)
                fi.other_sites.append(site)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def resolve(self, name: str) -> str | None:
        """Canonical qualified name for a query name, following imports and re-exports."""
        if name in self.defs:
            return name
        parts = name.split(".")
        for i in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in self.modules:
                walked = self._walk_module(prefix, parts[i:], set())
                if isinstance(walked, str):
                    return walked
                if isinstance(walked, tuple) and walked[0] == "module":
                    return self.modules[walked[1]].module
        return None

    def definitions(self, qualified_name: str) -> list[Symbol]:
        return list(self.defs.get(qualified_name, []))

    def references(self, qualified_name: str) -> list[Reference]:
        found: list[Reference] = []
        for fi in self.files.values():
            for imp in fi.imports:
                anchor = self._anchor_of_import(imp, fi)
                target = None
                if anchor is not None and anchor[0] == "module":
                    name_parts = [] if imp.name is None else imp.name.split(".")
                    walked = self._walk_module(anchor[1], name_parts, set())
                    if isinstance(walked, str):
                        target = walked
                    elif isinstance(walked, tuple) and walked[0] == "module":
                        target = self.modules[walked[1]].module
                if target == qualified_name:
                    found.append(Reference(qualified_name, "import", imp.file, imp.line, imp.scope))
        for site in self.all_call_sites():
            if site.resolution == "internal" and site.target == qualified_name:
                found.append(Reference(qualified_name, "call", site.file, site.line, site.scope))
        for site in self.all_other_sites():
            if site.resolution == "internal" and site.target == qualified_name:
                found.append(Reference(qualified_name, "other", site.file, site.line, site.scope))
        return found

    def callers(self, qualified_name: str) -> list[Symbol]:
        result: list[Symbol] = []
        for caller in self.graph.callers(qualified_name):
            result.extend(self.defs.get(caller, []))
        return result

    def call_sites(self, qualified_name: str) -> list[CallSite]:
        return [site for site in self.all_call_sites() if site.scope == qualified_name]

    def path(self, source: str, target: str) -> list[str] | None:
        return self.graph.shortest_path(source, target)

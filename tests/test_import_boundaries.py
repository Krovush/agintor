from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "agintor"

DELETED_FLAT_MODULES = {
    "archive",
    "artifacts",
    "benchmarks",
    "container_entry",
    "container_runtime",
    "crossover",
    "evaluator",
    "evolution",
    "exceptions",
    "factory_chat_store",
    "goal_rubric",
    "memory_graph",
    "mutator",
    "openai_trace",
    "patches",
    "predictors",
    "project",
    "prompt_builder",
    "prompts",
    "provider_common",
    "provider_minimax",
    "provider_openai",
    "run_store",
    "runner",
    "runtime_api",
    "runtime_builder",
    "runtime_host",
    "runtime_loader",
    "runtime_profile",
    "runtime_session_store",
    "schemas",
    "scoring",
    "shell",
    "state_store",
    "tool_runtime",
    "trace_labeler",
    "verifiers",
    "versioning",
}
DELETED_MODULES = {f"agintor.{name}" for name in DELETED_FLAT_MODULES}
DELETED_PREFIXES = ("agintor.runtime_sdk", "agintor.task_runtime")
OLD_IMPORT_TEXT_RE = re.compile(
    r"agintor\.(?:"
    + "|".join(re.escape(name) for name in sorted(DELETED_FLAT_MODULES))
    + r"|runtime_sdk|task_runtime)(?:\b|\.)"
)
OLD_BUNDLE_PATH_RE = re.compile(r"agintor_runtime/(?:task_runtime|runtime_sdk)(?:/|\b)")

INTENTIONAL_REEXPORT_FACADES = {
    "agintor.runtime.kernel.facade",
}
INTENTIONAL_PASS_THROUGH_FACADES: set[str] = set()
HOST_KERNEL_ENTRYPOINT_IMPORTS = {
    (
        "agintor.runtime.host.backends.docker.entrypoint",
        "agintor.runtime.kernel.facade",
    ),
    (
        "agintor.runtime.host.backends.docker.entrypoint",
        "agintor.runtime.kernel.shell",
    ),
}

REQUIRED_OWNER_MODULES = {
    "agintor.contracts.tracing",
    "agintor.contracts.providers",
    "agintor.contracts.factory",
    "agintor.contracts.benchmarks",
    "agintor.contracts.execution",
    "agintor.contracts.state",
    "agintor.contracts.checkpoints",
    "agintor.contracts.sessions",
    "agintor.contracts.runtime",
    "agintor.contracts.branches",
    "agintor.contracts.side_effects",
    "agintor.contracts.protocol",
    "agintor.contracts.search",
    "agintor.runtime.api.context",
    "agintor.runtime.api.request_loading",
    "agintor.runtime.api.prompt_intent",
    "agintor.runtime.api.capabilities",
    "agintor.runtime.api.tracing",
    "agintor.runtime.api.plan_nodes",
    "agintor.runtime.api.plan_compiler",
    "agintor.runtime.api.resume",
    "agintor.runtime.api.results",
    "agintor.runtime.api.protocol",
    "agintor.runtime.api.failures",
    "agintor.tracing.identity",
    "agintor.tracing.layout",
    "agintor.tracing.persistence",
    "agintor.tracing.materialization",
    "agintor.tracing.rendering",
    "agintor.storage.state_store.layout",
    "agintor.storage.state_store.connection",
    "agintor.storage.state_store.schema",
    "agintor.storage.state_store.store",
    "agintor.storage.state_store.indexers",
    "agintor.storage.state_store.memory",
    "agintor.storage.state_store.rebuild",
    "agintor.storage.state_store.queries",
    "agintor.storage.state_store.serializers",
    "agintor.runtime.tools.models",
    "agintor.runtime.tools.registry",
    "agintor.runtime.tools.sandbox",
    "agintor.runtime.tools.safety",
    "agintor.runtime.tools.executor",
    "agintor.runtime.tools.execution",
    "agintor.runtime.tools.validation",
    "agintor.runtime.host.backend_selection",
    "agintor.runtime.host.preflight",
    "agintor.runtime.host.resume_resolution",
    "agintor.runtime.host.finalization",
    "agintor.runtime.host.validation",
    "agintor.runtime.host.local_process",
    "agintor.runtime.host.backends.docker.image",
    "agintor.runtime.host.backends.docker.commands",
    "agintor.runtime.host.backends.docker.path_mapping",
    "agintor.runtime.host.backends.docker.request_rewrite",
    "agintor.runtime.host.backends.docker.checkpoint_rewrite",
    "agintor.runtime.host.backends.docker.run_rewrite",
    "agintor.runtime.host.backends.docker.response_rewrite",
    "agintor.runtime.kernel.branches.budget",
    "agintor.runtime.kernel.branches.providers",
    "agintor.runtime.kernel.branches.resume",
    "agintor.runtime.kernel.branches.execution",
    "agintor.runtime.kernel.branches.results",
    "agintor.runtime.kernel.checkpointing.restore",
    "agintor.runtime.kernel.checkpointing.publication",
    "agintor.runtime.kernel.checkpointing.snapshots",
    "agintor.runtime.kernel.checkpointing.recovery",
    "agintor.runtime.kernel.checkpointing.results",
    "agintor.runtime.kernel.io.paths",
    "agintor.runtime.kernel.io.repo_patch",
    "agintor.runtime.kernel.io.service_action",
    "agintor.runtime.kernel.loop",
    "agintor.runtime.kernel.root_frame",
    "agintor.runtime.kernel.progress",
    "agintor.factory.service",
    "agintor.factory.pipeline",
    "agintor.factory.workspace",
    "agintor.factory.planning",
    "agintor.factory.export",
    "agintor.factory.followups",
    "agintor.factory.trace_context",
}

FOCUSED_SIZE_LIMITS = {
    "agintor.factory.service": 300,
    "agintor.runtime.host.host": 450,
    "agintor.runtime.host.backends.docker.executor": 700,
    "agintor.storage.state_store.store": 200,
    "agintor.tracing.persistence": 250,
    "agintor.runtime.tools.executor": 120,
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = ("agintor", *rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolved_imports(path: Path) -> set[str]:
    if path.is_relative_to(PACKAGE_ROOT):
        module = _module_name(path)
        package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    else:
        package = ""
    imports: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names if alias.name.startswith("agintor"))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.level == 0:
                if node.module == "agintor":
                    imports.update(f"agintor.{alias.name}" for alias in node.names)
                elif node.module.startswith("agintor."):
                    imports.add(node.module)
                    imports.update(f"{node.module}.{alias.name}" for alias in node.names)
                continue
            if not package:
                continue
            package_parts = package.split(".")
            base_parts = package_parts[: max(0, len(package_parts) - node.level + 1)]
            imports.add(".".join((*base_parts, node.module)))
    return {name for name in imports if name == "agintor" or name.startswith("agintor.")}


def _is_deleted_import(module_name: str) -> bool:
    return (
        module_name in DELETED_MODULES
        or any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in DELETED_PREFIXES)
        or any(module_name.startswith(f"{deleted}.") for deleted in DELETED_MODULES)
    )


def _python_paths() -> list[Path]:
    return [*PACKAGE_ROOT.rglob("*.py"), *Path(__file__).resolve().parent.rglob("*.py")]


def _is_future_import(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__"


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _is_dunder_all_assignment(stmt: ast.stmt) -> bool:
    return isinstance(stmt, (ast.Assign, ast.AnnAssign)) and any(
        isinstance(target, ast.Name) and target.id == "__all__"
        for target in ([stmt.target] if isinstance(stmt, ast.AnnAssign) else stmt.targets)
    )


def _is_import_statement(stmt: ast.stmt) -> bool:
    return isinstance(stmt, (ast.Import, ast.ImportFrom))


def _semantic_body(tree: ast.Module) -> list[ast.stmt]:
    return [
        stmt
        for stmt in tree.body
        if not (
            _is_docstring(stmt)
            or _is_future_import(stmt)
            or _is_import_statement(stmt)
            or _is_dunder_all_assignment(stmt)
        )
    ]


def _base_name(base: ast.expr) -> str:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return ""


def _is_exception_marker_class(node: ast.ClassDef) -> bool:
    exception_base_names = {
        "Exception",
        "RuntimeError",
        "ValueError",
        "AgintorError",
        "HardInvalidation",
    }
    return node.name.endswith(("Error", "Exception")) or any(
        _base_name(base) in exception_base_names for base in node.bases
    )


def _is_pass_through_subclass_wrapper(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = _semantic_body(tree)
    if len(body) != 1 or not isinstance(body[0], ast.ClassDef):
        return False
    class_def = body[0]
    if _is_exception_marker_class(class_def):
        return False
    return bool(class_def.bases) and len(class_def.body) == 1 and isinstance(class_def.body[0], ast.Pass)


def _exports_real_owner_body(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if _is_pass_through_subclass_wrapper(path):
        return False
    for stmt in tree.body:
        if _is_docstring(stmt) or _is_future_import(stmt):
            continue
        if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and not _is_dunder_all_assignment(stmt):
            targets = [stmt.target] if isinstance(stmt, ast.AnnAssign) else stmt.targets
            if any(isinstance(target, ast.Name) and not target.id.startswith("_") for target in targets):
                return True
    return False


def _module_path(module: str) -> Path:
    rel = Path(*module.split(".")[1:])
    package_init = PACKAGE_ROOT / rel / "__init__.py"
    if package_init.exists():
        return package_init
    return (PACKAGE_ROOT / rel).with_suffix(".py")


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_canonical_import_surfaces_resolve():
    modules = [
        "agintor.contracts",
        "agintor.runtime.api",
        "agintor.runtime.host",
        "agintor.runtime.host.backends.docker",
        "agintor.runtime.kernel",
        "agintor.runtime.sdk",
        "agintor.runtime.tools",
        "agintor.storage.state_store",
        "agintor.tracing",
        "agintor.providers",
        "agintor.factory.service",
        "agintor.evaluation.evaluator",
        "agintor.search.engine",
    ]
    for module in modules:
        importlib.import_module(module)


def test_deleted_flat_modules_and_old_packages_are_absent():
    leftovers = [str(PACKAGE_ROOT / f"{name}.py") for name in sorted(DELETED_FLAT_MODULES) if (PACKAGE_ROOT / f"{name}.py").exists()]
    for package_name in ("runtime_sdk", "task_runtime"):
        package_path = PACKAGE_ROOT / package_name
        if package_path.exists():
            leftovers.append(str(package_path))
    assert leftovers == []


def test_no_source_or_test_imports_reference_deleted_paths():
    offenders: list[tuple[str, str]] = []
    for path in _python_paths():
        for imported in _resolved_imports(path):
            if _is_deleted_import(imported):
                offenders.append((str(path.relative_to(REPO_ROOT)), imported))
    assert offenders == []


def test_no_source_or_test_text_references_deleted_import_paths():
    offenders: list[tuple[str, str]] = []
    for path in _python_paths():
        if path == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in (OLD_IMPORT_TEXT_RE, OLD_BUNDLE_PATH_RE):
            for match in pattern.finditer(text):
                offenders.append((str(path.relative_to(REPO_ROOT)), match.group(0)))
    assert offenders == []


def test_non_init_modules_are_not_reexport_only_wrappers():
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        module = _module_name(path)
        if module in INTENTIONAL_REEXPORT_FACADES:
            continue
        if not _exports_real_owner_body(path):
            offenders.append(module)
    assert offenders == []


def test_non_init_modules_do_not_hide_pass_through_subclass_wrappers():
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        module = _module_name(path)
        if module in INTENTIONAL_PASS_THROUGH_FACADES:
            continue
        if _is_pass_through_subclass_wrapper(path):
            offenders.append(module)
    assert offenders == []


def test_required_owner_modules_exist_and_own_code():
    missing: list[str] = []
    wrapper_only: list[str] = []
    for module in sorted(REQUIRED_OWNER_MODULES):
        path = _module_path(module)
        if not path.exists():
            missing.append(module)
            continue
        if path.name != "__init__.py" and not _exports_real_owner_body(path):
            wrapper_only.append(module)
    assert missing == []
    assert wrapper_only == []


def test_known_monoliths_are_gone_or_focused():
    assert not (PACKAGE_ROOT / "contracts" / "models.py").exists()
    assert not (PACKAGE_ROOT / "runtime" / "kernel" / "checkpointing.py").exists()
    oversized = []
    for module, limit in sorted(FOCUSED_SIZE_LIMITS.items()):
        path = _module_path(module)
        if path.exists() and _line_count(path) > limit:
            oversized.append((module, _line_count(path), limit))
    assert oversized == []


def test_contracts_do_not_import_implementation_packages():
    forbidden_prefixes = (
        "agintor.factory",
        "agintor.runtime",
        "agintor.storage",
        "agintor.tracing",
        "agintor.providers",
        "agintor.learning",
        "agintor.evaluation",
        "agintor.search",
    )
    offenders: list[tuple[str, str]] = []
    for path in (PACKAGE_ROOT / "contracts").rglob("*.py"):
        module = _module_name(path)
        for imported in _resolved_imports(path):
            if imported.startswith(forbidden_prefixes):
                offenders.append((module, imported))
    assert offenders == []


def test_factory_evaluation_search_stay_off_runtime_kernel_internals():
    offenders: list[tuple[str, str]] = []
    for folder in ("factory", "evaluation", "search"):
        for path in (PACKAGE_ROOT / folder).rglob("*.py"):
            module = _module_name(path)
            for imported in _resolved_imports(path):
                if imported.startswith("agintor.runtime.kernel"):
                    offenders.append((module, imported))
    assert offenders == []


def test_runtime_host_stays_behind_execution_boundary():
    forbidden_prefixes = (
        "agintor.factory",
        "agintor.evaluation",
        "agintor.search",
        "agintor.runtime.kernel",
    )
    offenders: list[tuple[str, str]] = []
    for path in (PACKAGE_ROOT / "runtime" / "host").rglob("*.py"):
        module = _module_name(path)
        for imported in _resolved_imports(path):
            if imported.startswith(forbidden_prefixes):
                if (module, imported) in HOST_KERNEL_ENTRYPOINT_IMPORTS:
                    continue
                offenders.append((module, imported))
    assert offenders == []

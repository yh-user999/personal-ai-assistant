"""模块化单体的静态依赖方向检查。

目标层尚未全部落地时规则可以空跑；现有 legacy 包只对审计确认的反向边
保留逐条豁免，禁止把整个旧包作为永久白名单。
"""
from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
TARGET_LAYERS = {"contracts", "domain", "application", "infrastructure", "adapters"}
ALLOWED_LAYERS = {
    "contracts": {"contracts"},
    "domain": {"domain", "contracts"},
    "application": {"application", "domain", "contracts"},
    "infrastructure": {"infrastructure", "domain", "contracts"},
    "adapters": {"adapters", "application", "infrastructure", "domain", "contracts"},
}
DOMAIN_FORBIDDEN_EXTERNAL_ROOTS = {
    "fastapi",
    "starlette",
    "sqlite3",
    "httpx",
    "aiohttp",
    "mcp",
    "nonebot",
    "onebot",
    "napcat",
}

# 这些边是当前兼容层仍在使用的已知反向依赖。迁移删除某条边时，应同时删除
# 对应记录；同一敏感源模块出现新的目标边则必须显式审查并加入/修复，而不是
# 把整个包加入白名单。
LEGACY_EDGE_EXEMPTIONS = {
    ("app.models.database", "app.services.self_reflect"),  # 旧 lessons 迁移复用分类器
    ("app.chat.retrieval", "app.chat.prompting"),  # 提示词安全包装的延迟导入
    ("app.core.knowledge", "app.services.knowledge_domain"),  # 旧知识域分类器
    ("app.core.knowledge", "app.services.sanitize"),  # 旧入库脱敏工具
    ("app.core.memory", "app.services.sanitize"),  # 旧记忆入库脱敏工具
}
TARGET_LAYER_EDGE_EXEMPTIONS = {
    ("app.application.chat", "app.chat.context"),  # pipeline 迁移前复用旧请求契约
    ("app.application.chat", "app.chat.pipeline"),  # pipeline 迁移前复用旧编排
    ("app.application.chat", "app.chat.prompting"),  # 旧 prompt 导出兼容
    ("app.application.chat", "app.chat.routing"),  # 旧命令导出兼容
    ("app.application.ports", "app.core"),  # 单体组合根装配 legacy 端口实现
}
GUARDED_LEGACY_PREFIXES = {
    "app.models": ("app.core", "app.services"),
    "app.chat.retrieval": ("app.chat.prompting",),
    "app.core.knowledge": ("app.services",),
    "app.core.memory": ("app.services",),
}

# 2026-09-16 架构审计记录的包级双向耦合现状。这里只做"不许再增"的棘轮：
# 消除一组就从清单里删一组，新增一组会让测试失败，迫使显式审查而不是默默积累。
# 不强制立刻清零——收敛路径依赖 fitness/novel/group 的 application 门面迁移。
KNOWN_PACKAGE_CYCLES = {
    ("app.application", "app.chat"),  # ChatApplication 仍复用旧 chat 编排
    ("app.chat", "app.group"),  # 群聊轮次编排与聊天流水线互相回调
    ("app.core", "app.novel"),  # 旧知识/记忆与小说领域互相取数
    ("app.core", "app.services"),  # 混合共享层双向依赖
    ("app.models", "app.services"),  # 迁移逻辑复用服务层分类器
    ("app.novel", "app.services"),  # 小说领域复用脱敏/分类工具
}

# 函数体内 import 的数量上限（审计基线 229）。留少量余量吸收正常改动，
# 但显著增长会触发失败：延迟导入会把真实依赖藏起来，静态分析看不见。
MAX_DEFERRED_INTERNAL_IMPORTS = 235


def _module_name(path: Path) -> str:
    parts = path.relative_to(APP_ROOT).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(("app", *parts)) if parts else "app"


def _resolve_relative(module_name: str, node: ast.ImportFrom, *, is_package: bool) -> str:
    if node.level == 0:
        return node.module or ""
    package = module_name.split(".") if is_package else module_name.split(".")[:-1]
    levels_up = max(0, node.level - 1)
    if levels_up:
        package = package[:-levels_up]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _import_targets(module_name: str, tree: ast.AST, *, is_package: bool = False) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = _resolve_relative(module_name, node, is_package=is_package)
        if node.module or node.level == 0:
            if base:
                targets.add(base)
            continue
        targets.update(f"{base}.{alias.name}" for alias in node.names if base)
    return targets


def _internal_edges() -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for path in APP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        source = _module_name(path)
        for target in _import_targets(source, tree, is_package=path.name == "__init__.py"):
            if target.startswith("app.") and target != source:
                edges.add((source, target))
    return edges


def _layer_for(module_name: str) -> str | None:
    parts = module_name.split(".")
    if len(parts) > 1 and parts[0] == "app" and parts[1] in TARGET_LAYERS:
        return parts[1]
    return None


def test_target_layers_follow_declared_dependency_direction():
    violations: list[str] = []
    for source, target in _internal_edges():
        source_layer = _layer_for(source)
        if source_layer is None:
            continue
        target_layer = _layer_for(target)
        if target_layer not in ALLOWED_LAYERS[source_layer] and (
            source, target
        ) not in TARGET_LAYER_EDGE_EXEMPTIONS:
            violations.append(
                f"{source} -> {target}：{source_layer} 不得依赖 {target_layer or target}"
            )
    assert not violations, "\n".join(sorted(violations))


def test_domain_layer_has_no_transport_or_storage_sdk_dependency():
    violations: list[str] = []
    for source, target in _external_imports_from_target_layers():
        if _layer_for(source) != "domain":
            continue
        root = target.split(".", 1)[0].casefold()
        if root in DOMAIN_FORBIDDEN_EXTERNAL_ROOTS:
            violations.append(f"{source} -> {target}")
    assert not violations, "\n".join(sorted(violations))


def _external_imports_from_target_layers() -> set[tuple[str, str]]:
    imports: set[tuple[str, str]] = set()
    for path in APP_ROOT.rglob("*.py"):
        source = _module_name(path)
        if _layer_for(source) is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for target in _import_targets(source, tree, is_package=path.name == "__init__.py"):
            if not target.startswith("app."):
                imports.add((source, target))
    return imports


def test_sensitive_legacy_reverse_edges_are_explicitly_exempted():
    observed = _internal_edges()
    unexpected: set[tuple[str, str]] = set()
    for source, target in observed:
        for source_prefix, target_prefixes in GUARDED_LEGACY_PREFIXES.items():
            if source != source_prefix:
                continue
            if any(target == prefix or target.startswith(prefix + ".") for prefix in target_prefixes):
                if (source, target) not in LEGACY_EDGE_EXEMPTIONS:
                    unexpected.add((source, target))
    assert not unexpected, "未记录的 legacy 反向依赖：\n" + "\n".join(
        f"{source} -> {target}" for source, target in sorted(unexpected)
    )


def _package_of(module_name: str) -> str:
    parts = module_name.split(".")
    return ".".join(parts[:2]) if len(parts) > 1 else module_name


def _package_cycles() -> set[tuple[str, str]]:
    """包级双向依赖（A 依赖 B 且 B 依赖 A），忽略包内部边。"""
    outgoing: dict[str, set[str]] = {}
    for source, target in _internal_edges():
        source_pkg, target_pkg = _package_of(source), _package_of(target)
        if source_pkg != target_pkg:
            outgoing.setdefault(source_pkg, set()).add(target_pkg)
    return {
        tuple(sorted((a, b)))
        for a, targets in outgoing.items()
        for b in targets
        if a in outgoing.get(b, ())
    }


def _deferred_internal_imports() -> dict[str, int]:
    """统计函数体内的 app.* import（每个模块的次数）。"""
    counts: dict[str, int] = {}
    for path in APP_ROOT.rglob("*.py"):
        source = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        inside_function: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        inside_function.add(id(child))
        total = 0
        for node in ast.walk(tree):
            if id(node) not in inside_function:
                continue
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [
                    _resolve_relative(
                        source, node, is_package=path.name == "__init__.py"
                    )
                ]
            else:
                continue
            total += sum(1 for t in targets if t.startswith("app") and t != source)
        if total:
            counts[source] = total
    return counts


def test_package_level_cycles_do_not_grow_beyond_recorded_baseline():
    """包级循环只减不增：新增一组必须显式审查并记录，不能默默积累。"""
    observed = _package_cycles()
    added = observed - KNOWN_PACKAGE_CYCLES
    assert not added, "新增包级双向耦合（需消除或显式记录）：\n" + "\n".join(
        f"{a} <-> {b}" for a, b in sorted(added)
    )
    resolved = KNOWN_PACKAGE_CYCLES - observed
    assert not resolved, (
        "以下循环已消除，请从 KNOWN_PACKAGE_CYCLES 删除，保持棘轮收紧：\n"
        + "\n".join(f"{a} <-> {b}" for a, b in sorted(resolved))
    )


def test_deferred_internal_imports_do_not_grow():
    """函数体内 import 会把真实依赖藏起来，总量不得显著增长。"""
    counts = _deferred_internal_imports()
    total = sum(counts.values())
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    assert total <= MAX_DEFERRED_INTERNAL_IMPORTS, (
        f"函数体内 app.* import 增至 {total}（上限 {MAX_DEFERRED_INTERNAL_IMPORTS}）。"
        "新增依赖请放模块顶层；确有循环需先解开或显式提高上限并说明原因。\n"
        "当前最多的模块：\n"
        + "\n".join(f"  {name}: {count}" for name, count in top)
    )


def test_models_repo_does_not_depend_on_core_memory():
    assert ("app.models.repo", "app.core.memory") not in _internal_edges()


def test_chat_http_and_routing_have_no_reverse_compatibility_edge():
    edges = _internal_edges()
    assert ("app.api.chat", "app.chat.routing") not in edges
    assert ("app.chat.routing", "app.api") not in edges


def test_mcp_adapter_does_not_depend_on_http_chat_adapter():
    assert not any(
        source.startswith("app.mcp") and target.startswith("app.api.chat")
        for source, target in _internal_edges()
    )


def test_mcp_adapter_uses_application_ports_instead_of_core_modules():
    assert not any(
        source.startswith("app.mcp")
        and (target == "app.core" or target.startswith("app.core."))
        for source, target in _internal_edges()
    )

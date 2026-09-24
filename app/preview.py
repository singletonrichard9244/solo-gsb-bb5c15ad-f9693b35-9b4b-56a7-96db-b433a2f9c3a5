"""注册表变更预演（只读、纯内存）。

在不触碰数据库的前提下，把一次 ``PUT /api/v1/registry/packages/{name}``
写入会产生的注册表快照在内存中构造出来，分别用**当前**注册表与**预演**
注册表求解同一份清单，对比两侧状态与可行时选中的版本。

预演沿用 PUT 的整包语义：

* 请求中的版本按主键逐个合并，同版本覆盖依赖；
* 目标包**未在请求中列出的其他版本原样保留**；
* 其他包完全不受影响。

任一侧求解器搜索预算耗尽（``inconclusive``）时，结论不可推断：
``status_changed`` 与版本解差异均置空，避免在“不知道”的情况下给出
变化结论。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

from .semver import Version, VersionError

Registry = Dict[str, Dict[str, Dict[str, str]]]


def build_preview_registry(
    registry: Registry,
    name: str,
    versions: List[Dict[str, Any]],
) -> Registry:
    """按 PUT 逐版本覆盖、其他版本保留的语义在内存构造预演注册表。

    返回全新的深拷贝（``{包: {版本: {依赖: 范围}}}``），调用方可自由修改，
    不影响传入的当前注册表。
    """
    preview: Registry = deepcopy(registry)
    package_versions = preview.setdefault(name, {})
    for item in versions:
        package_versions[item["version"]] = dict(item.get("dependencies", {}))
    return preview


def _version_sort_key(text: str) -> tuple:
    """可解析的 SemVer 按版本序，非法文本兜底字典序。"""
    try:
        return (0, Version.parse(text))
    except (VersionError, ValueError):
        return (1, text)


def package_version_changes(
    registry: Registry,
    preview: Registry,
    name: str,
    requested_versions: List[str],
) -> List[Dict[str, Any]]:
    """求目标包在预演中新增或被覆盖依赖的版本。

    仅比较本次请求显式列出的版本：当前不存在即“新增”（before=null）；
    当前存在但依赖映射不同即“覆盖”（before=旧依赖）；请求中重复出现的
    版本只报告一次。按 SemVer 升序输出。
    """
    before_versions = registry.get(name, {})
    after_versions = preview.get(name, {})
    changes: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for version in requested_versions:
        if version in seen:
            continue
        seen.add(version)
        before = before_versions.get(version)
        after = after_versions[version]
        if before is not None and before == after:
            continue
        changes.append(
            {
                "version": version,
                "before": None if before is None else dict(before),
                "after": dict(after),
            }
        )
    changes.sort(key=lambda item: _version_sort_key(item["version"]))
    return changes


def resolution_diff(
    current: Optional[Dict[str, str]],
    preview: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """对比两份可行版本解，按 added/removed/changed/unchanged 分区。

    键排序后输出，保证结果字节稳定。
    """
    current = current or {}
    preview = preview or {}
    added: Dict[str, str] = {}
    removed: Dict[str, str] = {}
    changed: Dict[str, str] = {}
    unchanged: Dict[str, str] = {}
    for pkg in sorted(set(current) | set(preview)):
        if pkg not in current:
            added[pkg] = preview[pkg]
        elif pkg not in preview:
            removed[pkg] = current[pkg]
        elif current[pkg] != preview[pkg]:
            changed[pkg] = preview[pkg]
        else:
            unchanged[pkg] = current[pkg]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
    }


def build_preview_body(
    name: str,
    requested_versions: List[str],
    current_result: Any,
    preview_result: Any,
    registry: Registry,
    preview: Registry,
) -> Dict[str, Any]:
    """组装预演响应体。

    ``current_result`` / ``preview_result`` 为两侧 :class:`~app.solver.Solver`
    的 ``SolveResult``。任一侧 inconclusive 时不推断状态与解的变化；
    两侧均为确定结论（feasible/infeasible）时即计算选中版本差异。
    """
    current_side = {
        "status": current_result.status,
        "resolution": current_result.resolution
        if current_result.status == "feasible"
        else None,
    }
    preview_side = {
        "status": preview_result.status,
        "resolution": preview_result.resolution
        if preview_result.status == "feasible"
        else None,
    }

    inconclusive = (
        current_result.status == "inconclusive"
        or preview_result.status == "inconclusive"
    )
    if inconclusive:
        # 搜索预算耗尽：结论不可推断，状态与解的变化一律不给
        status_changed: Optional[bool] = None
        diff: Optional[Dict[str, Any]] = None
    else:
        status_changed = current_result.status != preview_result.status
        # 两侧均为确定结论即可对比选中版本：infeasible 一侧没有版本解，
        # 另一侧的全部包计入 added（infeasible -> feasible）或 removed（feasible -> infeasible）
        diff = resolution_diff(
            current_result.resolution if current_result.status == "feasible" else None,
            preview_result.resolution if preview_result.status == "feasible" else None,
        )

    return {
        "name": name,
        "versions": list(requested_versions),
        "current": current_side,
        "preview": preview_side,
        "changes": {
            "status_changed": status_changed,
            "resolution": diff,
            "package_versions": package_version_changes(
                registry, preview, name, requested_versions
            ),
        },
    }

"""注册表版本退役评估（只读、纯内存）。

在不触碰数据库的前提下，把注册表中某个包的**单个具体版本**移除，得到
内存中的退役注册表快照；随后对每条给定的**已保存可行审计**，用其原始
清单在退役快照上重新求解，逐条返回：

* 原审计的求解状态与选中版本解；
* 退役后重跑的状态（``feasible`` / ``infeasible`` / ``inconclusive`` /
  ``unavailable``）；
* 原解与重跑解的选中版本差异。

特殊情形：

* **根插件版本被移除**：某条审计的清单把被退役版本声明为根插件
  （``plugin_name`` / ``plugin_version``）时，该版本不复存在，
  直接把该条标记为 ``unavailable``，不再运行求解器；
* **搜索不确定**：重跑超过求解器节点预算（``inconclusive``）时，
  结论不可推断，版本差异置空。

任一条不可用、无解或搜索不确定，总体 ``can_retire`` 都为 ``false``。
整个过程只在内存中进行，不写入注册表，也不写审计。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Dict, List

from .preview import resolution_diff

Registry = Dict[str, Dict[str, Dict[str, str]]]

_REASON_TEXT = {
    "unavailable": "根插件版本 {package}@{version} 已被移除，该审计的清单无法再求解",
    "infeasible": "退役 {package}@{version} 后原清单重新求解无解",
    "inconclusive": "退役 {package}@{version} 后重新求解超出搜索预算，结论不确定",
}


def build_retirement_registry(registry: Registry, package: str, version: str) -> Registry:
    """在内存中构造移除 ``package@version`` 后的注册表快照。

    返回全新的深拷贝（``{包: {版本: {依赖: 范围}}}``）；被移除版本是该包
    最后一个版本时，保留空的包条目（调用方据此判断版本是否存在），
    调用方可自由修改，不影响传入的当前注册表。
    """
    retired: Registry = deepcopy(registry)
    if package in retired:
        retired[package].pop(version, None)
    return retired


def evaluate_retirement(
    audits: List[Dict[str, Any]],
    registry: Registry,
    package: str,
    version: str,
    solver_factory: Callable[[Registry], Any],
) -> Dict[str, Any]:
    """逐条评估在退役注册表中重跑原清单的结果。

    :param audits: 已保存审计行（调用方负责保证存在且 ``status == feasible``），
        每项需含 ``id`` 与 ``manifest_json``；顺序即输出顺序。
    :param registry: 当前注册表（``{包: {版本: {依赖: 范围}}}``）。
    :param package: 拟退役的包名。
    :param version: 拟退役的版本字符串（调用方负责保证为合法 SemVer）。
    :param solver_factory: 给定注册表返回求解器的工厂（需提供
        ``solve(root_dependencies, root_name, root_version)``），
        便于测试注入低预算求解器。
    :return: 可直接序列化为 :class:`~app.models.RetirementPreviewResponse`
        的响应体（不含请求侧的计数字段）。
    """
    # 延迟导入避免与 models/solver 的潜在循环依赖
    from .models import ManifestIn

    retired_registry = build_retirement_registry(registry, package, version)
    solver = solver_factory(retired_registry)

    items: List[Dict[str, Any]] = []
    counts = {"feasible": 0, "infeasible": 0, "inconclusive": 0, "unavailable": 0}

    for index, row in enumerate(audits):
        audit_id = row["id"]
        result_json = row["result_json"]
        manifest = ManifestIn.model_validate(row["manifest_json"])

        item: Dict[str, Any] = {
            "index": index,
            "audit_id": audit_id,
            "original_status": "feasible",
            "original_resolution": dict(result_json.get("resolution") or {}),
            "rerun_status": None,
            "rerun_resolution": None,
            "resolution_diff": None,
            "reason": None,
        }

        # 根插件版本正是被退役版本：该条直接不可用，不再求解
        if (
            manifest.plugin_name is not None
            and manifest.plugin_version is not None
            and manifest.plugin_name == package
            and manifest.plugin_version == version
        ):
            rerun_status = "unavailable"
        else:
            rerun = solver.solve(
                root_dependencies=[(d.name, d.range) for d in manifest.dependencies],
                root_name=manifest.plugin_name,
                root_version=manifest.plugin_version,
            )
            rerun_status = rerun.status
            if rerun_status == "feasible":
                item["rerun_resolution"] = dict(rerun.resolution or {})

        item["rerun_status"] = rerun_status
        counts[rerun_status] += 1

        if rerun_status == "feasible":
            item["resolution_diff"] = resolution_diff(
                item["original_resolution"], item["rerun_resolution"]
            )
        elif rerun_status == "infeasible":
            # 结论确定地无解：重跑侧没有版本解，原解中的包整体计入 removed
            # （与注册表变更预演两侧对比语义一致）
            item["resolution_diff"] = resolution_diff(
                item["original_resolution"], None
            )
            item["reason"] = _REASON_TEXT[rerun_status].format(
                package=package, version=version
            )
        else:
            # unavailable / inconclusive：不重跑或结论不确定，不推断版本差异
            item["reason"] = _REASON_TEXT[rerun_status].format(
                package=package, version=version
            )

        items.append(item)

    can_retire = all(item["rerun_status"] == "feasible" for item in items)
    return {
        "package": package,
        "version": version,
        "can_retire": can_retire,
        "counts": {"total": len(audits), **counts},
        "items": items,
    }


def sorted_duplicate_ids(audit_ids: List[str]) -> List[str]:
    """稳定列出请求中重复出现（出现次数 > 1）的审计 ID。"""
    return sorted({aid for aid in audit_ids if audit_ids.count(aid) > 1})

"""注册表版本退役评估（只读、纯内存）。

给定一个待退役的 ``包@版本`` 与一组**已保存的可行审计**，在内存中构造
“移除该版本后”的注册表快照，对每条审计的**原清单**重新求解，评估该版本
是否可以安全退役：

* 退役注册表是当前注册表的深拷贝，仅删除 ``{package: {version}}`` 这一个键，
  其他版本与其他包（连同依赖声明）一律保留；
* 每条审计单独使用同一份退役注册表副本重跑求解器，互不影响；
* 审计的根插件（``plugin_name/plugin_version``）正是被退役版本时，该条直接
  标记为 ``unavailable``——根版本已不存在，清单无法再求解；
* 逐条对比**已保存的原解**与重跑结果；任一条 unavailable / infeasible /
  inconclusive 时总体 ``can_retire=false``；
* inconclusive（搜索预算耗尽）时结论不可推断，该条不计算选中版本差异。

整个过程只读数据库：不写入注册表，也不写入或修改任何审计记录。
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from .database import get_audit
from .models import ManifestIn
from .preview import resolution_diff
from .solver import Solver

Registry = Dict[str, Dict[str, Dict[str, str]]]

# 可接受的已保存审计状态
FEASIBLE = "feasible"


def dedupe_audit_ids(audit_ids: List[str]) -> List[str]:
    """按首次出现顺序去重审计 ID。"""
    seen: set[str] = set()
    unique: List[str] = []
    for audit_id in audit_ids:
        if audit_id in seen:
            continue
        seen.add(audit_id)
        unique.append(audit_id)
    return unique


def build_retirement_registry(
    registry: Registry, package: str, version: str
) -> Registry:
    """在内存构造退役注册表：仅移除 ``package`` 的 ``version``，其余深拷贝保留。

    调用前应已确认该版本存在；版本（甚至整个包）不存在时返回的快照与当前
    注册表等价（深拷贝）。
    """
    retired: Registry = deepcopy(registry)
    versions = retired.get(package)
    if versions is not None:
        versions.pop(version, None)
        if not versions:
            # 整个包再无任何版本：与 load_registry 的形态保持一致（无空键）
            retired.pop(package, None)
    return retired


def _rerun_manifest(
    manifest: ManifestIn, retired_registry: Registry
) -> Tuple[str, Optional[Dict[str, str]], Any]:
    """用退役注册表对原清单重新求解，返回 (status, resolution, result)。

    根插件版本已被移除时直接判定 unavailable，不进入求解器。
    """
    if (
        manifest.plugin_name is not None
        and manifest.plugin_version not in retired_registry.get(manifest.plugin_name, {})
    ):
        return "unavailable", None, None

    solver = Solver(retired_registry)
    result = solver.solve(
        root_dependencies=[(d.name, d.range) for d in manifest.dependencies],
        root_name=manifest.plugin_name,
        root_version=manifest.plugin_version,
    )
    resolution = result.resolution if result.status == FEASIBLE else None
    return result.status, resolution, result


def _changes_for(
    status: str,
    original_resolution: Optional[Dict[str, str]],
    rerun_resolution: Optional[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    """仅在重跑可行（结论确定）时推断版本差异，其余一律 None。"""
    if status != FEASIBLE:
        return None
    return resolution_diff(original_resolution or {}, rerun_resolution or {})


def evaluate_retirement(
    conn: sqlite3.Connection,
    registry: Registry,
    package: str,
    version: str,
    audit_ids: List[str],
) -> Dict[str, Any]:
    """逐条评估版本退役影响。

    调用前应已完成全部前置校验（版本存在、审计存在且 feasible、ID 已去重）。
    ``audit_ids`` 的顺序即响应 ``items`` 的顺序。整个函数不执行任何写事务。
    """
    retired_registry = build_retirement_registry(registry, package, version)

    items: List[Dict[str, Any]] = []
    counts = {"feasible": 0, "infeasible": 0, "inconclusive": 0, "unavailable": 0}

    for audit_id in audit_ids:
        row = get_audit(conn, audit_id)
        # 前置校验已保证行存在且状态为 feasible；此处防御性处理不应发生的缺失
        if row is None:
            raise LookupError(audit_id)

        saved = json.loads(row["result_json"])
        original_resolution = saved.get("resolution")
        manifest = ManifestIn.model_validate(json.loads(row["manifest_json"]))
        # 已保存审计落库前通过了根一致性校验；这里仍复核一次，坏数据不致 500
        manifest.validate_root_consistency()

        status, rerun_resolution, _ = _rerun_manifest(manifest, retired_registry)
        counts[status] += 1

        items.append(
            {
                "audit_id": audit_id,
                "original": {
                    "status": saved["status"],
                    "resolution": original_resolution,
                },
                "rerun": {"status": status, "resolution": rerun_resolution},
                "changes": _changes_for(status, original_resolution, rerun_resolution),
            }
        )

    return {
        "package": package,
        "version": version,
        "audit_ids": list(audit_ids),
        "total": len(audit_ids),
        "feasible": counts["feasible"],
        "infeasible": counts["infeasible"],
        "inconclusive": counts["inconclusive"],
        "unavailable": counts["unavailable"],
        "can_retire": counts["feasible"] == len(audit_ids),
        "items": items,
    }

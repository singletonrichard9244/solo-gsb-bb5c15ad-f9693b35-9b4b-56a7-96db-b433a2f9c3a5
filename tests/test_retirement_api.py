"""注册表版本退役评估接口测试：纯函数与 HTTP 端到端。"""

from app import main as main_module
from app.retirement import (
    build_retirement_registry,
    evaluate_retirement,
    sorted_duplicate_ids,
)
from app.solver import DEFAULT_NODE_BUDGET, Solver

PATH = "/api/v1/registry/retirements/preview"


def _make_audit(client, dependencies=None, plugin_name=None, plugin_version=None):
    """提交一条审计，返回 (audit_id, status, resolution)。"""
    manifest: dict = {"dependencies": dependencies or []}
    if plugin_name is not None:
        manifest["plugin_name"] = plugin_name
        manifest["plugin_version"] = plugin_version
    resp = client.post("/api/v1/audits", json=manifest)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], body["status"], body["resolution"]


def _preview(client, package, version, audit_ids):
    return client.post(
        PATH, json={"package": package, "version": version, "audit_ids": audit_ids}
    )


# ---------- 纯函数 ----------


def test_build_retirement_registry_removes_only_named_version():
    registry = {
        "core-lib": {"1.0.0": {}, "1.5.0": {}},
        "other": {"1.0.0": {}},
    }
    retired = build_retirement_registry(registry, "core-lib", "1.0.0")
    assert set(retired["core-lib"]) == {"1.5.0"}
    assert retired["other"] == {"1.0.0": {}}
    # 深拷贝：修改退役快照不影响当前注册表
    retired["core-lib"]["9.9.9"] = {}
    retired["other"]["1.0.0"]["x"] = "^1.0.0"
    assert "9.9.9" not in registry["core-lib"]
    assert registry["other"]["1.0.0"] == {}


def test_build_retirement_registry_unknown_package_is_noop_copy():
    registry = {"p": {"1.0.0": {}}}
    retired = build_retirement_registry(registry, "nope", "1.0.0")
    assert retired == registry
    retired["p"]["1.0.0"]["d"] = "^1"
    assert registry["p"]["1.0.0"] == {}


def test_sorted_duplicate_ids():
    assert sorted_duplicate_ids(["a", "b", "a", "c", "b"]) == ["a", "b"]
    assert sorted_duplicate_ids(["a", "b", "c"]) == []


def test_evaluate_retirement_pure_reruns_and_diff():
    # 不经过 HTTP，直接构造审计行验证纯函数
    audits = [
        {
            "id": "a1",
            "result_json": {"resolution": {"core-lib": "1.5.0"}},
            "manifest_json": {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
        },
        {
            "id": "a2",
            "result_json": {"resolution": {"core-lib": "2.1.0"}},
            "manifest_json": {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]},
        },
    ]
    registry = {
        "core-lib": {
            "1.0.0": {},
            "1.5.0": {},
            "2.0.0": {},
            "2.1.0": {},
        }
    }
    body = evaluate_retirement(
        audits, registry, "core-lib", "1.5.0", solver_factory=lambda reg: Solver(reg)
    )
    assert body["package"] == "core-lib"
    assert body["version"] == "1.5.0"
    assert body["can_retire"] is True
    assert body["counts"] == {
        "total": 2,
        "feasible": 2,
        "infeasible": 0,
        "inconclusive": 0,
        "unavailable": 0,
    }
    # 1.5.0 被移除后 ^1.0.0 下移到 1.0.0
    first, second = body["items"]
    assert first["audit_id"] == "a1"
    assert first["rerun_status"] == "feasible"
    assert first["rerun_resolution"] == {"core-lib": "1.0.0"}
    assert first["resolution_diff"]["changed"] == {"core-lib": "1.0.0"}
    assert first["reason"] is None
    # ^2.0.0 的解完全不受影响
    assert second["rerun_resolution"] == {"core-lib": "2.1.0"}
    assert second["resolution_diff"]["unchanged"] == {"core-lib": "2.1.0"}
    assert second["resolution_diff"]["changed"] == {}


def test_evaluate_retirement_root_plugin_version_marked_unavailable():
    audits = [
        {
            "id": "r1",
            "result_json": {"resolution": {"mega": "1.0.0"}},
            "manifest_json": {
                "plugin_name": "mega",
                "plugin_version": "1.0.0",
                "dependencies": [],
            },
        }
    ]
    registry = {"mega": {"1.0.0": {"dep": "^1.0.0"}, "2.0.0": {}}, "dep": {"1.0.0": {}}}
    calls = []

    class _NoRunSolver(Solver):
        def solve(self, *args, **kwargs):  # pragma: no cover - 不应被调用
            calls.append(args)
            raise AssertionError("根插件版本被移除时不应再运行求解器")

    body = evaluate_retirement(
        audits, registry, "mega", "1.0.0", solver_factory=lambda reg: _NoRunSolver(reg)
    )
    assert calls == []
    item = body["items"][0]
    assert item["rerun_status"] == "unavailable"
    assert item["rerun_resolution"] is None
    assert item["resolution_diff"] is None
    assert "mega@1.0.0" in item["reason"]
    assert body["can_retire"] is False
    assert body["counts"]["unavailable"] == 1


def test_evaluate_retirement_infeasible_rerun_lists_removed_diff():
    audits = [
        {
            "id": "i1",
            "result_json": {"resolution": {"p": "1.0.0"}},
            "manifest_json": {"dependencies": [{"name": "p", "range": "1.0.0"}]},
        }
    ]
    # 退役唯一满足 =1.0.0 的版本 -> 重跑确定无解
    registry = {"p": {"1.0.0": {}, "2.0.0": {}}}
    body = evaluate_retirement(
        audits, registry, "p", "1.0.0", solver_factory=lambda reg: Solver(reg)
    )
    item = body["items"][0]
    assert item["rerun_status"] == "infeasible"
    assert item["rerun_resolution"] is None
    assert item["resolution_diff"]["removed"] == {"p": "1.0.0"}
    assert item["resolution_diff"]["added"] == {}
    assert item["resolution_diff"]["changed"] == {}
    assert item["reason"] is not None
    assert body["can_retire"] is False


def test_evaluate_retirement_inconclusive_infers_no_diff():
    audits = [
        {
            "id": "u1",
            "result_json": {"resolution": {"core-lib": "1.5.0"}},
            "manifest_json": {"dependencies": [{"name": "core-lib", "range": "*"}]},
        }
    ]
    registry = {
        "core-lib": {"1.0.0": {}, "1.5.0": {}, "2.1.0": {}},
    }

    class _ZeroBudgetSolver(Solver):
        def __init__(self, registry, node_budget=DEFAULT_NODE_BUDGET):
            super().__init__(registry, node_budget=0)

    body = evaluate_retirement(
        audits, registry, "core-lib", "1.5.0",
        solver_factory=lambda reg: _ZeroBudgetSolver(reg),
    )
    item = body["items"][0]
    assert item["rerun_status"] == "inconclusive"
    assert item["rerun_resolution"] is None
    # 搜索不确定：不推断版本差异
    assert item["resolution_diff"] is None
    assert item["reason"] is not None
    assert body["can_retire"] is False
    assert body["counts"]["inconclusive"] == 1


# ---------- HTTP 接口 ----------


def test_retirement_preview_safe_version_can_retire(client):
    # loggerkit 0.5.0 被 httpkit 2.2.0 使用；退役一个不被任何审计选中的版本
    audit_id, status, resolution = _make_audit(
        client, [{"name": "core-lib", "range": "^1.0.0"}]
    )
    assert status == "feasible"
    assert resolution == {"core-lib": "1.5.0"}

    # 退役 1.4.0：^1.0.0 的解仍为 1.5.0，不受影响
    resp = _preview(client, "core-lib", "1.4.0", [audit_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["package"] == "core-lib"
    assert body["version"] == "1.4.0"
    assert body["requested_audit_count"] == 1
    assert body["duplicate_audit_ids"] == []
    assert body["can_retire"] is True
    assert body["counts"] == {
        "total": 1, "feasible": 1, "infeasible": 0, "inconclusive": 0, "unavailable": 0
    }
    item = body["items"][0]
    assert item["index"] == 0
    assert item["audit_id"] == audit_id
    assert item["original_status"] == "feasible"
    assert item["original_resolution"] == {"core-lib": "1.5.0"}
    assert item["rerun_status"] == "feasible"
    assert item["rerun_resolution"] == {"core-lib": "1.5.0"}
    assert item["resolution_diff"]["unchanged"] == {"core-lib": "1.5.0"}
    assert item["resolution_diff"]["changed"] == {}
    assert item["reason"] is None


def test_retirement_preview_moves_selected_version(client):
    # 退役当前选中的 1.5.0：^1.0.0 的解下移到 1.4.0（仍可行）
    audit_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    resp = _preview(client, "core-lib", "1.5.0", [audit_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is True
    item = body["items"][0]
    assert item["rerun_status"] == "feasible"
    assert item["rerun_resolution"] == {"core-lib": "1.4.0"}
    assert item["resolution_diff"]["changed"] == {"core-lib": "1.4.0"}
    assert item["resolution_diff"]["unchanged"] == {}


def test_retirement_preview_makes_infeasible(client):
    # core-lib =1.0.0 是精确约束；退役 1.0.0 后确定无解
    audit_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "1.0.0"}])
    resp = _preview(client, "core-lib", "1.0.0", [audit_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is False
    assert body["counts"]["infeasible"] == 1
    item = body["items"][0]
    assert item["rerun_status"] == "infeasible"
    assert item["rerun_resolution"] is None
    assert item["resolution_diff"]["removed"] == {"core-lib": "1.0.0"}
    assert item["reason"] is not None


def test_retirement_preview_transitive_dependency_removed(client):
    # 组合清单选中 cachekit 1.3.0（core-lib ^1.4.0）；退役 1.3.0 后
    # cachekit ^1.0.0 下移到 1.1.0（core-lib >=1.0.0 <2.0.0），其余包不变
    audit_id, _, resolution = _make_audit(
        client,
        [
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "httpkit", "range": "~2.2.0"},
        ],
    )
    assert resolution["cachekit"] == "1.3.0"
    assert resolution["httpkit"] == "2.2.0"
    resp = _preview(client, "cachekit", "1.3.0", [audit_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is True
    item = body["items"][0]
    assert item["rerun_status"] == "feasible"
    rerun = item["rerun_resolution"]
    assert rerun["cachekit"] == "1.1.0"
    assert rerun["httpkit"] == "2.2.0"
    diff = item["resolution_diff"]
    assert diff["changed"] == {"cachekit": "1.1.0"}
    assert diff["added"] == {}
    assert diff["removed"] == {}
    assert diff["unchanged"]["httpkit"] == "2.2.0"


def test_retirement_preview_root_plugin_version_unavailable(client):
    # megaplugin@1.0.0 作为根插件；退役该版本 -> unavailable（不求解）
    audit_id, status, _ = _make_audit(
        client, [], plugin_name="megaplugin", plugin_version="1.0.0"
    )
    assert status == "feasible"
    resp = _preview(client, "megaplugin", "1.0.0", [audit_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is False
    assert body["counts"]["unavailable"] == 1
    item = body["items"][0]
    assert item["rerun_status"] == "unavailable"
    assert item["rerun_resolution"] is None
    assert item["resolution_diff"] is None
    assert "根插件版本" in item["reason"]
    assert "megaplugin@1.0.0" in item["reason"]


def test_retirement_preview_duplicate_ids_deduped_in_input_order(client):
    a_id, _, a_res = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, b_res = _make_audit(client, [{"name": "core-lib", "range": "~1.4.0"}])

    resp = _preview(client, "core-lib", "1.5.0", [b_id, a_id, b_id, a_id, a_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested_audit_count"] == 5
    assert body["duplicate_audit_ids"] == sorted({a_id, b_id})
    assert set(body["duplicate_audit_ids"]) == {a_id, b_id}
    assert body["counts"]["total"] == 2
    # 顺序按首次出现：b 在前 a 在后
    assert [it["audit_id"] for it in body["items"]] == [b_id, a_id]
    # b 的 ~1.4.0 在 1.5.0 退役前后均为 1.4.0（不变）
    assert body["items"][0]["original_resolution"] == b_res
    assert body["items"][0]["rerun_resolution"] == {"core-lib": "1.4.0"}
    # a 的 ^1.0.0 从 1.5.0 下移 1.4.0
    assert body["items"][1]["original_resolution"] == a_res
    assert body["items"][1]["rerun_resolution"] == {"core-lib": "1.4.0"}


def test_retirement_preview_mixed_results_overall_blocked(client):
    safe_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^2.0.0"}])
    blocked_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "1.0.0"}])
    resp = _preview(client, "core-lib", "1.0.0", [safe_id, blocked_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is False
    assert body["counts"] == {
        "total": 2, "feasible": 1, "infeasible": 1, "inconclusive": 0, "unavailable": 0
    }
    assert [it["rerun_status"] for it in body["items"]] == ["feasible", "infeasible"]
    assert body["items"][0]["reason"] is None
    assert body["items"][1]["reason"] is not None


def test_retirement_preview_inconclusive_blocks_and_no_diff(client, monkeypatch):
    audit_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "*"}])

    # 所有重跑求解器预算归零 -> inconclusive
    class _ZeroSolver(Solver):
        def __init__(self, registry, node_budget=DEFAULT_NODE_BUDGET):
            super().__init__(registry, node_budget=0)

    monkeypatch.setattr(main_module, "Solver", _ZeroSolver)
    resp = _preview(client, "core-lib", "1.5.0", [audit_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is False
    assert body["counts"]["inconclusive"] == 1
    item = body["items"][0]
    assert item["rerun_status"] == "inconclusive"
    assert item["resolution_diff"] is None
    assert item["reason"] is not None


def test_retirement_preview_unknown_package_version_404(client):
    audit_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    resp = _preview(client, "core-lib", "9.9.9", [audit_id])
    assert resp.status_code == 404
    assert "9.9.9" in resp.json()["detail"]

    resp = _preview(client, "nope-pkg", "1.0.0", [audit_id])
    assert resp.status_code == 404
    assert "nope-pkg" in resp.json()["detail"]


def test_retirement_preview_unknown_audit_404(client):
    # 版本存在但审计不存在
    resp = _preview(client, "core-lib", "1.5.0", ["00000000-0000-0000-0000-000000000000"])
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "00000000" in detail

    # 一个存在一个不存在：仍 404 且不执行评估
    audit_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    resp = _preview(
        client, "core-lib", "1.5.0",
        [audit_id, "11111111-1111-1111-1111-111111111111"],
    )
    assert resp.status_code == 404
    assert "11111111" in resp.json()["detail"]


def test_retirement_preview_non_feasible_audit_422(client):
    # cachekit ^1 + loggerkit ^0.9 在种子注册表上无解
    bad_id, bad_status, _ = _make_audit(
        client,
        [
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ],
    )
    assert bad_status == "infeasible"
    resp = _preview(client, "core-lib", "1.5.0", [bad_id])
    assert resp.status_code == 422
    assert bad_id in resp.json()["detail"]

    # 可行 + 不可行混合：整体 422
    good_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    resp = _preview(client, "core-lib", "1.5.0", [good_id, bad_id])
    assert resp.status_code == 422
    assert bad_id in resp.json()["detail"]


def test_retirement_preview_invalid_semver_422(client):
    audit_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    resp = _preview(client, "core-lib", "not-semver", [audit_id])
    assert resp.status_code == 422


def test_retirement_preview_audit_ids_count_limits_422(client):
    # 空列表
    resp = client.post(
        PATH, json={"package": "core-lib", "version": "1.5.0", "audit_ids": []}
    )
    assert resp.status_code == 422

    # 超过 20 个（即使重复，原始数量也受限）
    audit_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    resp = client.post(
        PATH,
        json={"package": "core-lib", "version": "1.5.0", "audit_ids": [audit_id] * 21},
    )
    assert resp.status_code == 422


def test_retirement_preview_missing_fields_422(client):
    resp = client.post(PATH, json={"package": "core-lib", "version": "1.5.0"})
    assert resp.status_code == 422
    resp = client.post(PATH, json={"version": "1.5.0", "audit_ids": ["x"]})
    assert resp.status_code == 422
    resp = client.post(PATH, json={"package": "core-lib", "audit_ids": ["x"]})
    assert resp.status_code == 422


def test_retirement_preview_blank_audit_id_422(client):
    resp = _preview(client, "core-lib", "1.5.0", ["  "])
    assert resp.status_code == 422


def test_retirement_preview_is_read_only(client):
    packages_before = client.get("/api/v1/registry/packages").json()
    audits_before = client.get("/api/v1/audits").json()["total"]

    audit_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    for _ in range(2):
        resp = _preview(client, "core-lib", "1.5.0", [audit_id])
        assert resp.status_code == 200

    assert client.get("/api/v1/registry/packages").json() == packages_before
    assert client.get("/api/v1/audits").json()["total"] == audits_before + 1


def test_retirement_preview_uses_original_manifest(client):
    # 评估重跑必须用审计保存时的原清单：同一审计重复评估结果稳定，
    # 且退役后注册表变化只影响该原清单的求解
    audit_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    first = _preview(client, "core-lib", "1.5.0", [audit_id]).json()
    second = _preview(client, "core-lib", "1.5.0", [audit_id]).json()
    assert first["items"] == second["items"]


def test_retirement_listed_in_index(client):
    endpoints = client.get("/").json()["endpoints"]
    assert "POST /api/v1/registry/retirements/preview" in endpoints


def test_retirement_openapi_doc_present(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/registry/retirements/preview"]["post"]
    assert operation["tags"] == ["registry"]
    assert "退役" in operation["summary"]
    assert "RetirementPreviewResponse" in schema["components"]["schemas"]
    # rerun_status 的四种取值都在枚举里
    rerun_status = schema["components"]["schemas"]["RetirementItemOut"]["properties"][
        "rerun_status"
    ]
    assert set(rerun_status["enum"]) == {
        "feasible", "infeasible", "inconclusive", "unavailable"
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

"""注册表变更预演接口测试：纯函数与 HTTP 端到端。"""

import pytest

from app import main as main_module
from app.preview import (
    build_preview_body,
    build_preview_registry,
    package_version_changes,
    resolution_diff,
)
from app.solver import DEFAULT_NODE_BUDGET, Solver

PREVIEW_PATH = "/api/v1/registry/packages/core-lib/preview"


# ---------- 纯函数 ----------


def test_build_preview_registry_overwrites_listed_and_keeps_others():
    registry = {
        "core-lib": {
            "1.0.0": {},
            "2.0.0": {"old": "^1.0.0"},
        },
        "other": {"1.0.0": {}},
    }
    preview = build_preview_registry(
        registry,
        "core-lib",
        [
            {"version": "2.0.0", "dependencies": {"newdep": "^2.0.0"}},
            {"version": "3.0.0", "dependencies": {}},
        ],
    )
    # 列出的版本被覆盖/新增
    assert preview["core-lib"]["2.0.0"] == {"newdep": "^2.0.0"}
    assert preview["core-lib"]["3.0.0"] == {}
    # 未列出的同包版本与其他包原样保留
    assert preview["core-lib"]["1.0.0"] == {}
    assert preview["other"] == {"1.0.0": {}}
    # 深拷贝：修改预演不影响当前注册表
    preview["core-lib"]["2.0.0"]["newdep"] = "^9.0.0"
    preview["other"]["9.9.9"] = {}
    assert registry["core-lib"]["2.0.0"] == {"old": "^1.0.0"}
    assert "9.9.9" not in registry["other"]


def test_package_version_changes_lists_new_and_overwritten_sorted():
    registry = {"p": {"1.10.0": {"a": "^1.0.0"}, "1.2.0": {}}}
    preview = build_preview_registry(
        registry,
        "p",
        [
            {"version": "2.0.0", "dependencies": {}},
            {"version": "1.10.0", "dependencies": {"a": "^2.0.0"}},
            {"version": "1.2.0", "dependencies": {}},  # 依赖未变，不计入变化
            {"version": "2.0.0", "dependencies": {}},  # 重复版本只报告一次
        ],
    )
    changes = package_version_changes(
        registry, preview, "p", ["2.0.0", "1.10.0", "1.2.0", "2.0.0"]
    )
    # SemVer 数值序：1.10.0 在 2.0.0 之前
    assert [c["version"] for c in changes] == ["1.10.0", "2.0.0"]
    assert changes[0] == {
        "version": "1.10.0",
        "before": {"a": "^1.0.0"},
        "after": {"a": "^2.0.0"},
    }
    assert changes[1] == {"version": "2.0.0", "before": None, "after": {}}


def test_resolution_diff_partitions():
    diff = resolution_diff(
        {"keep": "1.0.0", "move": "1.0.0", "gone": "2.0.0"},
        {"keep": "1.0.0", "move": "2.0.0", "born": "3.0.0"},
    )
    assert diff == {
        "added": {"born": "3.0.0"},
        "removed": {"gone": "2.0.0"},
        "changed": {"move": "2.0.0"},
        "unchanged": {"keep": "1.0.0"},
    }


class _Result:
    def __init__(self, status, resolution=None):
        self.status = status
        self.resolution = resolution


def test_build_preview_body_feasible_to_infeasible():
    registry = {"p": {}}
    preview = build_preview_registry(registry, "p", [{"version": "1.0.0"}])
    body = build_preview_body(
        "p",
        ["1.0.0"],
        _Result("feasible", {"p": "0.9.0"}),
        _Result("infeasible"),
        registry,
        preview,
    )
    assert body["current"] == {"status": "feasible", "resolution": {"p": "0.9.0"}}
    assert body["preview"] == {"status": "infeasible", "resolution": None}
    assert body["changes"]["status_changed"] is True
    # 结论确定（无 inconclusive）即可对比选中版本：解中的包全部被移除
    assert body["changes"]["resolution"] == {
        "added": {},
        "removed": {"p": "0.9.0"},
        "changed": {},
        "unchanged": {},
    }


def test_build_preview_body_inconclusive_either_side_infers_nothing():
    registry = {"p": {}}
    preview = build_preview_registry(registry, "p", [{"version": "1.0.0"}])

    def make(cur, prev):
        return build_preview_body(
            "p", ["1.0.0"], _Result(*cur), _Result(*prev), registry, preview
        )

    # 仅当前侧 inconclusive
    body = make(
        ("inconclusive",),
        ("feasible", {"p": "1.0.0"}),
    )
    assert body["current"]["resolution"] is None
    assert body["changes"]["status_changed"] is None
    assert body["changes"]["resolution"] is None

    # 仅预演侧 inconclusive
    body = make(
        ("feasible", {"p": "0.9.0"}),
        ("inconclusive",),
    )
    assert body["changes"]["status_changed"] is None
    assert body["changes"]["resolution"] is None

    # 两侧都 inconclusive
    body = make(("inconclusive",), ("inconclusive",))
    assert body["changes"]["status_changed"] is None
    assert body["changes"]["resolution"] is None


# ---------- HTTP 接口 ----------


def test_preview_new_version_makes_infeasible_feasible(client):
    # 种子注册表中 cachekit ^1 与 loggerkit ^0.9 在 core-lib 上不可兼得；
    # 预演覆盖 loggerkit@0.9.0 的依赖到 core-lib ^1.0.0 后变为可行。
    resp = client.post(
        "/api/v1/registry/packages/loggerkit/preview",
        json={
            "name": "loggerkit",
            "versions": [{"version": "0.9.0", "dependencies": {"core-lib": "^1.0.0"}}],
            "manifest": {
                "dependencies": [
                    {"name": "cachekit", "range": "^1.0.0"},
                    {"name": "loggerkit", "range": "^0.9.0"},
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "loggerkit"
    assert body["versions"] == ["0.9.0"]
    assert body["current"]["status"] == "infeasible"
    assert body["current"]["resolution"] is None
    assert body["preview"]["status"] == "feasible"
    assert body["preview"]["resolution"]["loggerkit"] == "0.9.0"
    assert body["preview"]["resolution"]["core-lib"].startswith("1.")
    assert body["changes"]["status_changed"] is True
    # 结论确定即可对比版本解：当前无解，预演解中的包全部计入 added
    diff = body["changes"]["resolution"]
    assert diff is not None
    assert diff["added"] == body["preview"]["resolution"]
    assert diff["removed"] == {}
    assert diff["changed"] == {}
    assert diff["unchanged"] == {}
    # 目标包版本变化：0.9.0 被覆盖（before 为旧依赖）
    pv = body["changes"]["package_versions"]
    assert pv == [
        {
            "version": "0.9.0",
            "before": {"core-lib": "^2.0.0"},
            "after": {"core-lib": "^1.0.0"},
        }
    ]


def test_preview_added_version_moves_feasible_resolution(client):
    # httpkit 当前最高 3.0.0；预演新增 4.0.0 后可行解上移
    resp = client.post(
        "/api/v1/registry/packages/httpkit/preview",
        json={
            "name": "httpkit",
            "versions": [{"version": "4.0.0", "dependencies": {"loggerkit": "^0.9.0"}}],
            "manifest": {"dependencies": [{"name": "httpkit", "range": "*"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == "feasible"
    assert body["current"]["resolution"]["httpkit"] == "3.0.0"
    assert body["preview"]["status"] == "feasible"
    assert body["preview"]["resolution"]["httpkit"] == "4.0.0"
    assert body["changes"]["status_changed"] is False
    diff = body["changes"]["resolution"]
    assert diff is not None
    assert diff["changed"] == {"httpkit": "4.0.0"}
    assert "loggerkit" in diff["unchanged"]
    assert diff["added"] == {}
    assert diff["removed"] == {}
    assert body["changes"]["package_versions"] == [
        {"version": "4.0.0", "before": None, "after": {"loggerkit": "^0.9.0"}}
    ]


def test_preview_feasible_both_sides_unchanged_when_write_irrelevant(client):
    # 新增 core-lib 3.0.0 不影响 ^1.0.0 约束的解
    resp = client.post(
        PREVIEW_PATH,
        json={
            "name": "core-lib",
            "versions": [{"version": "3.0.0", "dependencies": {}}],
            "manifest": {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == body["preview"]["status"] == "feasible"
    assert body["current"]["resolution"] == body["preview"]["resolution"] == {
        "core-lib": "1.5.0"
    }
    assert body["changes"]["status_changed"] is False
    assert body["changes"]["resolution"]["unchanged"] == {"core-lib": "1.5.0"}
    assert body["changes"]["resolution"]["changed"] == {}


def test_preview_new_package_version(client):
    # 目标包尚不存在：预演后新出现，其他包不受影响
    resp = client.post(
        "/api/v1/registry/packages/brand-new/preview",
        json={
            "name": "brand-new",
            "versions": [{"version": "1.0.0", "dependencies": {"core-lib": "^2.0.0"}}],
            "manifest": {"dependencies": [{"name": "brand-new", "range": "^1.0.0"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == "infeasible"
    assert body["preview"]["status"] == "feasible"
    assert body["preview"]["resolution"] == {
        "brand-new": "1.0.0",
        "core-lib": "2.1.0",
    }
    assert body["changes"]["package_versions"] == [
        {"version": "1.0.0", "before": None, "after": {"core-lib": "^2.0.0"}}
    ]


def test_preview_keeps_unlisted_versions(client):
    # 只列出 core-lib 3.0.0；预演侧旧版本仍可被 ~1.4.0 选中
    resp = client.post(
        PREVIEW_PATH,
        json={
            "name": "core-lib",
            "versions": [{"version": "3.0.0", "dependencies": {}}],
            "manifest": {"dependencies": [{"name": "core-lib", "range": "~1.4.0"}]},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["preview"]["status"] == "feasible"
    assert body["preview"]["resolution"]["core-lib"] == "1.4.0"


def test_preview_does_not_write_registry_or_audits(client):
    packages_before = client.get("/api/v1/registry/packages").json()
    audits_before = client.get("/api/v1/audits").json()["total"]

    for _ in range(2):
        resp = client.post(
            "/api/v1/registry/packages/loggerkit/preview",
            json={
                "name": "loggerkit",
                "versions": [
                    {"version": "0.9.0", "dependencies": {"core-lib": "^1.0.0"}},
                    {"version": "1.0.0", "dependencies": {}},
                ],
                "manifest": {
                    "dependencies": [{"name": "loggerkit", "range": "*"}]
                },
            },
        )
        assert resp.status_code == 200

    assert client.get("/api/v1/registry/packages").json() == packages_before
    assert client.get("/api/v1/audits").json()["total"] == audits_before
    # 注册表中确实没有被预演“写入”的 1.0.0
    versions = client.get("/api/v1/registry/packages").json()["packages"]["loggerkit"][
        "versions"
    ]
    assert "1.0.0" not in versions


def test_preview_path_name_mismatch_422(client):
    resp = client.post(
        "/api/v1/registry/packages/loggerkit/preview",
        json={
            "name": "core-lib",
            "versions": [{"version": "1.0.0", "dependencies": {}}],
            "manifest": {"dependencies": []},
        },
    )
    assert resp.status_code == 422
    assert "name" in resp.json()["detail"]


def test_preview_invalid_version_payload_422(client):
    resp = client.post(
        PREVIEW_PATH,
        json={
            "name": "core-lib",
            "versions": [{"version": "not-semver", "dependencies": {}}],
            "manifest": {"dependencies": []},
        },
    )
    assert resp.status_code == 422


def test_preview_invalid_dependency_range_422(client):
    resp = client.post(
        PREVIEW_PATH,
        json={
            "name": "core-lib",
            "versions": [{"version": "3.0.0", "dependencies": {"x": "bad-range"}}],
            "manifest": {"dependencies": []},
        },
    )
    assert resp.status_code == 422


def test_preview_empty_versions_422(client):
    resp = client.post(
        PREVIEW_PATH,
        json={
            "name": "core-lib",
            "versions": [],
            "manifest": {"dependencies": []},
        },
    )
    assert resp.status_code == 422


def test_preview_invalid_manifest_range_422(client):
    resp = client.post(
        PREVIEW_PATH,
        json={
            "name": "core-lib",
            "versions": [{"version": "3.0.0", "dependencies": {}}],
            "manifest": {
                "dependencies": [{"name": "core-lib", "range": "nope"}]
            },
        },
    )
    assert resp.status_code == 422


def test_preview_missing_manifest_field_422(client):
    resp = client.post(
        PREVIEW_PATH,
        json={
            "name": "core-lib",
            "versions": [{"version": "3.0.0", "dependencies": {}}],
        },
    )
    assert resp.status_code == 422


def test_preview_root_plugin_missing_422(client):
    resp = client.post(
        "/api/v1/registry/packages/megaplugin/preview",
        json={
            "name": "megaplugin",
            "versions": [{"version": "2.0.0", "dependencies": {}}],
            "manifest": {
                "plugin_name": "megaplugin",
                "plugin_version": "9.9.9",
                "dependencies": [],
            },
        },
    )
    # 根版本 9.9.9 在当前与预演注册表都不存在（预演仅含 2.0.0）-> 422
    assert resp.status_code == 422


def test_preview_root_plugin_inconsistent_422(client):
    resp = client.post(
        PREVIEW_PATH,
        json={
            "name": "core-lib",
            "versions": [{"version": "3.0.0", "dependencies": {}}],
            "manifest": {"plugin_name": "core-lib", "dependencies": []},
        },
    )
    assert resp.status_code == 422


def test_preview_root_plugin_resolves_on_both_sides(client):
    # 根插件 megaplugin@1.0.0 两侧都存在；预演改写无关包不影响其求解
    resp = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={
            "name": "core-lib",
            "versions": [{"version": "3.0.0", "dependencies": {}}],
            "manifest": {
                "plugin_name": "megaplugin",
                "plugin_version": "1.0.0",
                "dependencies": [],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == body["preview"]["status"] == "feasible"
    assert (
        body["current"]["resolution"]["megaplugin"]
        == body["preview"]["resolution"]["megaplugin"]
        == "1.0.0"
    )
    assert body["changes"]["resolution"]["changed"] == {}


def test_preview_budget_exhausted_keeps_inconclusive(client, monkeypatch):
    # 让求解器在“当前侧”预算耗尽、“预演侧”正常：不允许推断任何变化结论
    class _AlternatingSolver(Solver):
        _call = 0

        def __init__(self, registry, node_budget=DEFAULT_NODE_BUDGET):
            _AlternatingSolver._call += 1
            budget = 0 if _AlternatingSolver._call == 1 else DEFAULT_NODE_BUDGET
            super().__init__(registry, node_budget=budget)

    monkeypatch.setattr(main_module, "Solver", _AlternatingSolver)
    _AlternatingSolver._call = 0  # 防止类属性在同会话其他用例间串扰

    resp = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={
            "name": "core-lib",
            "versions": [{"version": "3.0.0", "dependencies": {}}],
            "manifest": {"dependencies": [{"name": "core-lib", "range": "*"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == "inconclusive"
    assert body["current"]["resolution"] is None
    assert body["preview"]["status"] == "feasible"
    # 任一侧 inconclusive：保留状态，不推断变化
    assert body["changes"]["status_changed"] is None
    assert body["changes"]["resolution"] is None
    # 注册表层面的版本变化仍然是确定事实，照常给出
    assert body["changes"]["package_versions"] == [
        {"version": "3.0.0", "before": None, "after": {}}
    ]
    # 预演依旧只读
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_preview_listed_in_index(client):
    endpoints = client.get("/").json()["endpoints"]
    assert "POST /api/v1/registry/packages/{name}/preview" in endpoints


def test_preview_openapi_doc_present(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/registry/packages/{name}/preview"]["post"]
    assert operation["tags"] == ["registry"]
    assert "预演" in operation["summary"]
    assert "RegistryPreviewResponse" in schema["components"]["schemas"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

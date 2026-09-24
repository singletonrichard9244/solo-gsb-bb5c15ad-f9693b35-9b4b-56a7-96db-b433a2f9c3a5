"""注册表版本退役评估接口测试：纯函数与 HTTP 端到端。"""

import pytest

from app import main as main_module
from app.retirements import (
    build_retirement_registry,
    dedupe_audit_ids,
    evaluate_retirement,
)
from app.solver import DEFAULT_NODE_BUDGET, Solver

RETIRE_PATH = "/api/v1/registry/retirements/preview"


def _create_audit(client, payload):
    resp = client.post("/api/v1/audits", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "feasible", body
    return body


# ---------- 纯函数 ----------


def test_dedupe_audit_ids_keeps_first_seen_order():
    assert dedupe_audit_ids(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
    assert dedupe_audit_ids([]) == []


def test_build_retirement_registry_removes_only_target_version():
    registry = {
        "core-lib": {"1.0.0": {}, "1.5.0": {"x": "^1.0.0"}},
        "other": {"1.0.0": {}},
    }
    retired = build_retirement_registry(registry, "core-lib", "1.0.0")
    # 仅目标版本被移除；同包其他版本与其他包保留
    assert retired == {"core-lib": {"1.5.0": {"x": "^1.0.0"}}, "other": {"1.0.0": {}}}
    # 深拷贝：退役快照与当前注册表互不影响
    retired["core-lib"]["1.5.0"]["x"] = "^9.0.0"
    retired["other"]["9.9.9"] = {}
    assert registry["core-lib"]["1.5.0"]["x"] == "^1.0.0"
    assert "9.9.9" not in registry["other"]


def test_build_retirement_registry_drops_package_when_last_version_removed():
    registry = {"lonely": {"1.0.0": {}}, "other": {"1.0.0": {}}}
    assert build_retirement_registry(registry, "lonely", "1.0.0") == {
        "other": {"1.0.0": {}}
    }
    # 目标版本/包不存在：快照与当前等价（仍是独立深拷贝）
    same = build_retirement_registry(registry, "lonely", "2.0.0")
    assert same == registry
    same["lonely"]["2.0.0"] = {}
    assert "2.0.0" not in registry["lonely"]


def test_evaluate_retirement_pure_flow(client):
    # 端到端走一遍纯函数：取连接 + 注册表，按已保存审计重跑
    core = _create_audit(
        client, {"dependencies": [{"name": "core-lib", "range": "~1.5.0"}]}
    )
    # 退役 1.5.0：~1.5.0 在剩余版本中无满足项（1.4.0 不满足）-> infeasible
    from app import database

    registry = database.load_registry(main_module.app.state.conn)
    body = evaluate_retirement(
        main_module.app.state.conn, registry, "core-lib", "1.5.0", [core["id"]]
    )
    assert body["can_retire"] is False
    assert body["total"] == body["infeasible"] == 1
    assert body["items"][0]["rerun"] == {"status": "infeasible", "resolution": None}
    assert body["items"][0]["changes"] is None
    assert body["items"][0]["original"]["resolution"] == {"core-lib": "1.5.0"}


# ---------- HTTP：基本场景 ----------


def test_retirement_all_still_feasible_can_retire(client):
    # core-lib 1.0.0 不被任何可行解选中：退役后解不变，can_retire=true
    audit = _create_audit(
        client, {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]}
    )
    resp = client.post(
        RETIRE_PATH,
        json={"package": "core-lib", "version": "1.0.0", "audit_ids": [audit["id"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["package"] == "core-lib"
    assert body["version"] == "1.0.0"
    assert body["requested_audit_ids"] == [audit["id"]]
    assert body["audit_ids"] == [audit["id"]]
    assert body["total"] == 1
    assert body["feasible"] == 1
    assert body["infeasible"] == body["inconclusive"] == body["unavailable"] == 0
    assert body["can_retire"] is True
    item = body["items"][0]
    assert item["audit_id"] == audit["id"]
    assert item["original"] == {
        "status": "feasible",
        "resolution": {"core-lib": "2.1.0"},
    }
    assert item["rerun"] == {"status": "feasible", "resolution": {"core-lib": "2.1.0"}}
    assert item["changes"] == {
        "added": {},
        "removed": {},
        "changed": {},
        "unchanged": {"core-lib": "2.1.0"},
    }


def test_retirement_feasible_but_resolution_moves(client):
    # 退役 core-lib 1.5.0：^1.4.0 的解从 1.5.0 下移到 1.4.0，仍可行但解移动
    audit = _create_audit(
        client, {"dependencies": [{"name": "core-lib", "range": "^1.4.0"}]}
    )
    assert audit["resolution"]["core-lib"] == "1.5.0"
    resp = client.post(
        RETIRE_PATH,
        json={"package": "core-lib", "version": "1.5.0", "audit_ids": [audit["id"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is True
    assert body["feasible"] == 1
    item = body["items"][0]
    assert item["rerun"]["status"] == "feasible"
    assert item["rerun"]["resolution"] == {"core-lib": "1.4.0"}
    assert item["changes"] == {
        "added": {},
        "removed": {},
        "changed": {"core-lib": "1.4.0"},
        "unchanged": {},
    }


def test_retirement_makes_audit_infeasible_blocks_retirement(client):
    # 退役 core-lib 1.5.0：~1.5.0 在剩余版本中无可满足版本 -> infeasible
    audit = _create_audit(
        client, {"dependencies": [{"name": "core-lib", "range": "~1.5.0"}]}
    )
    resp = client.post(
        RETIRE_PATH,
        json={"package": "core-lib", "version": "1.5.0", "audit_ids": [audit["id"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is False
    assert body["infeasible"] == 1
    assert body["feasible"] == body["inconclusive"] == body["unavailable"] == 0
    item = body["items"][0]
    assert item["original"]["status"] == "feasible"
    assert item["original"]["resolution"] == {"core-lib": "1.5.0"}
    assert item["rerun"] == {"status": "infeasible", "resolution": None}
    # 无解时不推断版本差异
    assert item["changes"] is None


def test_retirement_root_plugin_version_removed_marks_unavailable(client):
    # 根插件 megaplugin@1.0.0 正是被退役版本 -> unavailable，不进入求解器
    audit = _create_audit(
        client,
        {
            "plugin_name": "megaplugin",
            "plugin_version": "1.0.0",
            "dependencies": [],
        },
    )
    resp = client.post(
        RETIRE_PATH,
        json={
            "package": "megaplugin",
            "version": "1.0.0",
            "audit_ids": [audit["id"]],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is False
    assert body["unavailable"] == 1
    assert body["feasible"] == body["infeasible"] == body["inconclusive"] == 0
    item = body["items"][0]
    assert item["rerun"] == {"status": "unavailable", "resolution": None}
    assert item["changes"] is None
    assert item["original"]["resolution"]["megaplugin"] == "1.0.0"


def test_retirement_transitive_dependency_moves_still_feasible(client):
    # 退役 loggerkit 0.5.0：megaplugin 经 httpkit 2.2.0 需要 loggerkit >=0.5 <0.9，
    # 0.5.0 移除后无其他满足版本（0.4.0 不满足），整棵树无解 -> infeasible
    audit = _create_audit(
        client,
        {"plugin_name": "megaplugin", "plugin_version": "1.0.0", "dependencies": []},
    )
    resp = client.post(
        RETIRE_PATH,
        json={"package": "loggerkit", "version": "0.5.0", "audit_ids": [audit["id"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_retire"] is False
    assert body["infeasible"] == 1
    assert body["items"][0]["rerun"]["status"] == "infeasible"
    assert body["items"][0]["changes"] is None


def test_retirement_multiple_audits_dedup_and_order(client):
    a = _create_audit(client, {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]})
    b = _create_audit(client, {"dependencies": [{"name": "core-lib", "range": "~1.5.0"}]})
    c = _create_audit(
        client,
        {"plugin_name": "megaplugin", "plugin_version": "1.0.0", "dependencies": []},
    )
    resp = client.post(
        RETIRE_PATH,
        json={
            "package": "core-lib",
            "version": "1.5.0",
            # 故意重复并打乱：a 不受影响（feasible），b 变 infeasible；
            # c 的根不是 core-lib，重跑后传递解中的 core-lib 下移到 1.4.0，仍 feasible
            "audit_ids": [a["id"], b["id"], a["id"], c["id"], b["id"]],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 去重后 3 条，顺序按首次出现
    assert body["requested_audit_ids"] == [
        a["id"], b["id"], a["id"], c["id"], b["id"]
    ]
    assert body["audit_ids"] == [a["id"], b["id"], c["id"]]
    assert body["total"] == 3
    assert [item["audit_id"] for item in body["items"]] == [a["id"], b["id"], c["id"]]
    # a 可行（解不变）；b 直接约束 ~1.5.0，1.5.0 移除后无解；
    # c 的根不是 core-lib（不算 unavailable），重跑后 core-lib 下移到 1.4.0 仍可行
    assert body["feasible"] == 2
    assert body["infeasible"] == 1
    assert body["unavailable"] == body["inconclusive"] == 0
    assert body["can_retire"] is False
    assert [item["rerun"]["status"] for item in body["items"]] == [
        "feasible",
        "infeasible",
        "feasible",
    ]
    # c 的选中版本移动被如实报告
    moved = body["items"][2]
    assert moved["changes"]["changed"] == {"core-lib": "1.4.0"}
    assert moved["rerun"]["resolution"]["core-lib"] == "1.4.0"


# ---------- HTTP：错误码 ----------


def test_retirement_missing_version_404(client):
    audit = _create_audit(
        client, {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]}
    )
    resp = client.post(
        RETIRE_PATH,
        json={"package": "core-lib", "version": "9.9.9", "audit_ids": [audit["id"]]},
    )
    assert resp.status_code == 404
    assert "core-lib@9.9.9" in resp.json()["detail"]


def test_retirement_missing_package_404(client):
    audit = _create_audit(
        client, {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]}
    )
    resp = client.post(
        RETIRE_PATH,
        json={"package": "ghost-pkg", "version": "1.0.0", "audit_ids": [audit["id"]]},
    )
    assert resp.status_code == 404


def test_retirement_missing_audit_404(client):
    resp = client.post(
        RETIRE_PATH,
        json={
            "package": "core-lib",
            "version": "1.5.0",
            "audit_ids": ["does-not-exist"],
        },
    )
    assert resp.status_code == 404
    assert "审计记录不存在" in resp.json()["detail"]


def test_retirement_infeasible_audit_422(client):
    # cachekit ^1 + loggerkit ^0.9 在种子注册表上无解
    resp = client.post(
        "/api/v1/audits",
        json={
            "dependencies": [
                {"name": "cachekit", "range": "^1.0.0"},
                {"name": "loggerkit", "range": "^0.9.0"},
            ]
        },
    )
    assert resp.status_code == 201
    bad = resp.json()
    assert bad["status"] == "infeasible"

    retire = client.post(
        RETIRE_PATH,
        json={
            "package": "core-lib",
            "version": "1.5.0",
            "audit_ids": [bad["id"]],
        },
    )
    assert retire.status_code == 422
    assert bad["id"] in retire.json()["detail"]
    assert "feasible" in retire.json()["detail"]


def test_retirement_non_feasible_audit_in_batch_422(client):
    good = _create_audit(
        client, {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]}
    )
    resp = client.post(
        "/api/v1/audits",
        json={
            "dependencies": [
                {"name": "cachekit", "range": "^1.0.0"},
                {"name": "loggerkit", "range": "^0.9.0"},
            ]
        },
    )
    bad = resp.json()
    # 去重后含一条不可行审计 -> 422（即使另一条可行）
    out = client.post(
        RETIRE_PATH,
        json={
            "package": "core-lib",
            "version": "1.5.0",
            "audit_ids": [good["id"], bad["id"], good["id"]],
        },
    )
    assert out.status_code == 422


def test_retirement_version_404_precedes_audit_404(client):
    # 版本与审计都不存在时，先报版本 404
    resp = client.post(
        RETIRE_PATH,
        json={
            "package": "core-lib",
            "version": "9.9.9",
            "audit_ids": ["nope"],
        },
    )
    assert resp.status_code == 404
    assert "core-lib@9.9.9" in resp.json()["detail"]


def test_retirement_invalid_version_semver_422(client):
    resp = client.post(
        RETIRE_PATH,
        json={
            "package": "core-lib",
            "version": "not-semver",
            "audit_ids": ["x"],
        },
    )
    assert resp.status_code == 422


def test_retirement_empty_audit_ids_422(client):
    resp = client.post(
        RETIRE_PATH,
        json={"package": "core-lib", "version": "1.5.0", "audit_ids": []},
    )
    assert resp.status_code == 422


def test_retirement_too_many_audit_ids_422(client):
    ids = [f"id-{i}" for i in range(21)]
    resp = client.post(
        RETIRE_PATH,
        json={"package": "core-lib", "version": "1.5.0", "audit_ids": ids},
    )
    assert resp.status_code == 422


def test_retirement_blank_audit_id_422(client):
    resp = client.post(
        RETIRE_PATH,
        json={"package": "core-lib", "version": "1.5.0", "audit_ids": ["  "]},
    )
    assert resp.status_code == 422


def test_retirement_missing_fields_422(client):
    resp = client.post(RETIRE_PATH, json={"package": "core-lib", "version": "1.5.0"})
    assert resp.status_code == 422


# ---------- 搜索不确定 ----------


def test_retirement_inconclusive_infers_no_diff_and_blocks(client, monkeypatch):
    # 让评估期间的重跑求解器预算耗尽（retirements 模块直接持有 Solver 名字）
    audit = _create_audit(
        client, {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]}
    )

    from app import retirements as retirements_module

    real_init = Solver.__init__

    def patched_init(self, registry, node_budget=DEFAULT_NODE_BUDGET):
        # 预算 0：第一个搜索节点即耗尽 -> inconclusive
        real_init(self, registry, node_budget=0)

    monkeypatch.setattr(
        retirements_module,
        "Solver",
        type("PatchedSolver", (Solver,), {"__init__": patched_init}),
    )

    resp = client.post(
        RETIRE_PATH,
        json={"package": "core-lib", "version": "1.0.0", "audit_ids": [audit["id"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"][0]["rerun"]["status"] == "inconclusive"
    assert body["items"][0]["rerun"]["resolution"] is None
    # 搜索不确定：不推断版本差异，总体不能退役
    assert body["items"][0]["changes"] is None
    assert body["inconclusive"] == 1
    assert body["can_retire"] is False


# ---------- 只读 ----------


def test_retirement_does_not_write_registry_or_audits(client):
    audit = _create_audit(
        client, {"dependencies": [{"name": "core-lib", "range": "~1.5.0"}]}
    )
    packages_before = client.get("/api/v1/registry/packages").json()
    audits_before = client.get("/api/v1/audits").json()["total"]

    for _ in range(2):
        resp = client.post(
            RETIRE_PATH,
            json={
                "package": "core-lib",
                "version": "1.5.0",
                "audit_ids": [audit["id"]],
            },
        )
        assert resp.status_code == 200

    assert client.get("/api/v1/registry/packages").json() == packages_before
    assert client.get("/api/v1/audits").json()["total"] == audits_before
    # 被评估的审计记录内容不被修改（原解仍在）
    detail = client.get(f"/api/v1/audits/{audit['id']}").json()
    assert detail["status"] == "feasible"
    assert detail["resolution"] == {"core-lib": "1.5.0"}


def test_retirement_works_with_audit_created_via_batch(client):
    resp = client.post(
        "/api/v1/audits/batch",
        json={"manifests": [{"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]}]},
    )
    assert resp.status_code == 200
    audit_id = resp.json()["items"][0]["audit_id"]
    out = client.post(
        RETIRE_PATH,
        json={"package": "core-lib", "version": "1.0.0", "audit_ids": [audit_id]},
    )
    assert out.status_code == 200, out.text
    assert out.json()["can_retire"] is True


# ---------- 目录与文档 ----------


def test_retirement_listed_in_index(client):
    endpoints = client.get("/").json()["endpoints"]
    assert "POST /api/v1/registry/retirements/preview" in endpoints


def test_retirement_openapi_doc_present(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/registry/retirements/preview"]["post"]
    assert operation["tags"] == ["registry"]
    assert "退役" in operation["summary"]
    assert "RetirementPreviewResponse" in schema["components"]["schemas"]
    assert "RetirementItemOut" in schema["components"]["schemas"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

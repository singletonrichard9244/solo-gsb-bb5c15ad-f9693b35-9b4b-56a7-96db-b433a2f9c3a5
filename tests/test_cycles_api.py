"""注册表循环依赖诊断接口测试：纯函数与 HTTP 端到端。"""

import pytest

from app.cycles import build_registry_cycles


# ---------- 纯函数 ----------


def test_empty_registry_returns_empty():
    assert build_registry_cycles({}) == {"total": 0, "components": []}


def test_dag_registry_returns_empty():
    registry = {
        "core": {"1.0.0": {}, "2.0.0": {}},
        "leaf": {"1.0.0": {"core": "^1.0.0"}},
        "app": {"1.0.0": {"leaf": "^1.0.0", "core": ">=1.0.0"}},
    }
    assert build_registry_cycles(registry) == {"total": 0, "components": []}


def test_self_dependency_satisfying_range_is_reported():
    registry = {
        "loopy": {
            "1.0.0": {"loopy": "^1.0.0"},
            "2.0.0": {"loopy": "^1.0.0"},  # 不满足自身 2.x，不形成自环
        },
    }
    body = build_registry_cycles(registry)
    assert body["total"] == 1
    component = body["components"][0]
    assert component["nodes"] == [{"name": "loopy", "version": "1.0.0"}]
    assert component["edges"] == [
        {
            "source": {"name": "loopy", "version": "1.0.0"},
            "target": {"name": "loopy", "version": "1.0.0"},
            "version_range": "^1.0.0",
        }
    ]


def test_self_dependency_unsatisfied_range_not_a_cycle():
    registry = {"loopy": {"1.0.0": {"loopy": "^2.0.0"}}}
    assert build_registry_cycles(registry) == {"total": 0, "components": []}


def test_two_package_cycle_components_and_edges():
    registry = {
        "alpha": {"1.0.0": {"beta": "^2.0.0"}},
        "beta": {
            "1.0.0": {},
            "2.0.0": {"alpha": "^1.0.0"},
        },
    }
    body = build_registry_cycles(registry)
    assert body["total"] == 1
    component = body["components"][0]
    assert component["nodes"] == [
        {"name": "alpha", "version": "1.0.0"},
        {"name": "beta", "version": "2.0.0"},
    ]
    # 仅报告分量内的边；beta@1.0.0 无出边且不在环中
    assert component["edges"] == [
        {
            "source": {"name": "alpha", "version": "1.0.0"},
            "target": {"name": "beta", "version": "2.0.0"},
            "version_range": "^2.0.0",
        },
        {
            "source": {"name": "beta", "version": "2.0.0"},
            "target": {"name": "alpha", "version": "1.0.0"},
            "version_range": "^1.0.0",
        },
    ]


def test_range_fanout_edges_and_semver_sorting():
    # A 依赖 B ^1.0.0：同时匹配 B 的两个 1.x 版本；B 的各 1.x 又依赖 A
    registry = {
        "a": {"1.10.0": {"b": "^1.0.0"}},
        "b": {
            "1.2.0": {"a": "^1.0.0"},
            "1.10.0": {"a": "^1.0.0"},
        },
    }
    body = build_registry_cycles(registry)
    assert body["total"] == 1
    nodes = body["components"][0]["nodes"]
    assert nodes == [
        {"name": "a", "version": "1.10.0"},
        {"name": "b", "version": "1.2.0"},
        {"name": "b", "version": "1.10.0"},
    ]
    # 版本按 SemVer 数值序（1.2.0 < 1.10.0），而非字符串字典序
    edges = body["components"][0]["edges"]
    assert edges == [
        {
            "source": {"name": "a", "version": "1.10.0"},
            "target": {"name": "b", "version": "1.2.0"},
            "version_range": "^1.0.0",
        },
        {
            "source": {"name": "a", "version": "1.10.0"},
            "target": {"name": "b", "version": "1.10.0"},
            "version_range": "^1.0.0",
        },
        {
            "source": {"name": "b", "version": "1.2.0"},
            "target": {"name": "a", "version": "1.10.0"},
            "version_range": "^1.0.0",
        },
        {
            "source": {"name": "b", "version": "1.10.0"},
            "target": {"name": "a", "version": "1.10.0"},
            "version_range": "^1.0.0",
        },
    ]


def test_three_package_cycle_with_extra_dangling_versions():
    registry = {
        "a": {
            "1.0.0": {"b": "^1.0.0"},
            "0.9.0": {},  # 环外旧版本
        },
        "b": {"1.0.0": {"c": ">=1.0.0 <2.0.0"}},
        "c": {"1.5.0": {"a": "~1.0.0"}},
        "standalone": {"3.0.0": {"missing-pkg": "^1.0.0"}},  # 目标包未登记，无边
    }
    body = build_registry_cycles(registry)
    assert body["total"] == 1
    assert body["components"][0]["nodes"] == [
        {"name": "a", "version": "1.0.0"},
        {"name": "b", "version": "1.0.0"},
        {"name": "c", "version": "1.5.0"},
    ]
    assert len(body["components"][0]["edges"]) == 3


def test_multiple_components_sorted_by_first_node():
    registry = {
        "zzz": {"1.0.0": {"zzz": "*"}},
        "aaa": {"1.0.0": {"bbb": "*"}},
        "bbb": {"1.0.0": {"aaa": "*"}},
    }
    body = build_registry_cycles(registry)
    assert body["total"] == 2
    first_nodes = [comp["nodes"][0]["name"] for comp in body["components"]]
    assert first_nodes == ["aaa", "zzz"]


def test_prerelease_visibility_rules_apply_to_edges():
    # ^1.0.0 不匹配预发布版本 1.0.0-rc.1，因此只有正式版参与成环
    registry = {
        "a": {
            "1.0.0": {"b": "^1.0.0"},
            "1.0.0-rc.1": {"b": "^1.0.0"},
        },
        "b": {
            "1.0.0": {"a": "^1.0.0"},
            "1.0.0-rc.1": {"a": "^1.0.0"},
        },
    }
    body = build_registry_cycles(registry)
    assert body["total"] == 1
    assert body["components"][0]["nodes"] == [
        {"name": "a", "version": "1.0.0"},
        {"name": "b", "version": "1.0.0"},
    ]


# ---------- HTTP 接口 ----------


def test_cycles_endpoint_seed_registry_is_acyclic(client):
    resp = client.get("/api/v1/registry/cycles")
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "components": []}


def test_cycles_endpoint_listed_in_index(client):
    endpoints = client.get("/").json()["endpoints"]
    assert "GET /api/v1/registry/cycles" in endpoints


def test_cycles_endpoint_reports_injected_self_dependency(client):
    resp = client.put(
        "/api/v1/registry/packages/loopy",
        json={
            "name": "loopy",
            "versions": [{"version": "1.0.0", "dependencies": {"loopy": "^1.0.0"}}],
        },
    )
    assert resp.status_code == 200

    body = client.get("/api/v1/registry/cycles").json()
    assert body["total"] == 1
    assert body["components"][0]["nodes"] == [{"name": "loopy", "version": "1.0.0"}]
    edge = body["components"][0]["edges"][0]
    assert edge["source"] == edge["target"] == {"name": "loopy", "version": "1.0.0"}
    assert edge["version_range"] == "^1.0.0"


def test_cycles_endpoint_two_package_cycle_after_writes(client):
    for name, payload in {
        "cyc-a": {
            "name": "cyc-a",
            "versions": [{"version": "1.0.0", "dependencies": {"cyc-b": "^2.0.0"}}],
        },
        "cyc-b": {
            "name": "cyc-b",
            "versions": [{"version": "2.0.0", "dependencies": {"cyc-a": "^1.0.0"}}],
        },
    }.items():
        assert client.put(f"/api/v1/registry/packages/{name}", json=payload).status_code == 200

    body = client.get("/api/v1/registry/cycles").json()
    assert body["total"] == 1
    nodes = body["components"][0]["nodes"]
    assert [n["name"] for n in nodes] == ["cyc-a", "cyc-b"]


def test_cycles_endpoint_is_read_only(client):
    audits_before = client.get("/api/v1/audits").json()["total"]
    packages_before = client.get("/api/v1/registry/packages").json()["packages"]

    for _ in range(2):
        resp = client.get("/api/v1/registry/cycles")
        assert resp.status_code == 200

    assert client.get("/api/v1/audits").json()["total"] == audits_before
    assert client.get("/api/v1/registry/packages").json()["packages"] == packages_before


def test_cycles_endpoint_method_not_allowed_for_post(client):
    assert client.post("/api/v1/registry/cycles").status_code == 405


def test_cycles_endpoint_openapi_doc_present(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/registry/cycles"]["get"]
    assert "循环" in operation["summary"]
    assert operation["tags"] == ["registry"]
    assert "RegistryCyclesResponse" in schema["components"]["schemas"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

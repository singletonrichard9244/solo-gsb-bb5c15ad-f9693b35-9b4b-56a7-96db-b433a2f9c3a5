"""注册表循环依赖诊断（只读、纯内存）。

把注册表中**每个已登记的包版本**作为有向图节点：某版本声明的每条依赖
（包名 + SemVer 范围）连向注册表中**所有满足该范围**的目标版本；
目标包未登记或没有满足范围的版本时不产生边。

随后用 Tarjan 强连通分量（SCC）找出循环：

* 规模 >= 2 的 SCC 内节点互相可达，必然成环，分量内节点与分量内依赖边全部报告；
* 单节点 SCC 仅当存在**自环**（自依赖且自身版本满足声明范围）时作为循环报告。

节点按 (包名, SemVer 版本) 稳定排序，分量按其有序节点列表排序，
边按源节点、目标节点的名称与版本排序；注册表没有循环时返回空列表。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .semver import Version, VersionError, VersionRange

# 图节点：(包名, 版本字符串)
Node = Tuple[str, str]


def _version_sort_key(text: str) -> tuple:
    """与 impact 模块一致：可解析的 SemVer 按版本序，非法文本兜底字典序。"""
    try:
        return (0, Version.parse(text))
    except (VersionError, ValueError):
        return (1, text)


def _node_sort_key(node: Node) -> tuple:
    name, version = node
    return (name, _version_sort_key(version))


def _edge_sort_key(edge: Dict[str, Any]) -> tuple:
    source = edge["source"]
    target = edge["target"]
    return (
        source["name"],
        _version_sort_key(source["version"]),
        target["name"],
        _version_sort_key(target["version"]),
    )


def _tarjan_components(
    nodes: Sequence[Node],
    adjacency: Dict[Node, Dict[Node, str]],
) -> List[List[Node]]:
    """迭代版 Tarjan 强连通分量，避免深依赖链上的递归深度问题。

    邻居按 (包名, 版本) 稳定顺序遍历；分量按弹栈顺序返回，调用方自行排序。
    """
    indices: Dict[Node, int] = {}
    lowlinks: Dict[Node, int] = {}
    on_stack: set[Node] = set()
    stack: List[Node] = []
    components: List[List[Node]] = []
    counter = 0

    for root in nodes:
        if root in indices:
            continue
        indices[root] = lowlinks[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        # 工作帧：[节点, 下一待访问邻居下标]
        work: List[List[Any]] = [[root, 0]]
        while work:
            v, next_index = work[-1]
            neighbors = sorted(adjacency.get(v, ()), key=_node_sort_key)
            if next_index < len(neighbors):
                w = neighbors[next_index]
                work[-1][1] += 1
                if w not in indices:
                    indices[w] = lowlinks[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append([w, 0])
                elif w in on_stack:
                    if indices[w] < lowlinks[v]:
                        lowlinks[v] = indices[w]
            else:
                if lowlinks[v] == indices[v]:
                    component: List[Node] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == v:
                            break
                    components.append(component)
                work.pop()
                if work:
                    parent = work[-1][0]
                    if lowlinks[v] < lowlinks[parent]:
                        lowlinks[parent] = lowlinks[v]
    return components


def build_registry_cycles(
    registry: Dict[str, Dict[str, Dict[str, str]]],
) -> Dict[str, Any]:
    """在注册表快照上构图并返回循环依赖诊断响应体。

    ``registry`` 形如 ``{包名: {版本: {依赖名: 范围}}}``（即
    ``database.load_registry`` 的返回值）。纯内存计算，不触碰数据库。
    """
    # 预解析各包版本，供范围匹配使用
    parsed: Dict[str, List[Tuple[str, Optional[Version]]]] = {}
    for name, versions in registry.items():
        targets: List[Tuple[str, Optional[Version]]] = []
        for version_text in versions:
            try:
                targets.append((version_text, Version.parse(version_text)))
            except (VersionError, ValueError):
                targets.append((version_text, None))
        parsed[name] = targets

    # 构图：每个已登记版本一个节点，按依赖范围连向所有满足范围的已登记目标版本
    nodes: List[Node] = []
    adjacency: Dict[Node, Dict[Node, str]] = {}
    for name in sorted(registry):
        for version_text in sorted(registry[name], key=_version_sort_key):
            node: Node = (name, version_text)
            nodes.append(node)
            outgoing: Dict[Node, str] = {}
            dependencies = registry[name][version_text] or {}
            for dep_name in sorted(dependencies):
                range_text = dependencies[dep_name]
                try:
                    rng = VersionRange.parse(range_text)
                except VersionError:
                    # 写入路径已校验范围；防御性忽略非法范围，不产生边
                    continue
                for target_text, parsed_version in parsed.get(dep_name, []):
                    if parsed_version is not None and rng.satisfies(parsed_version):
                        # deps 以包名为键，同一目标版本至多一条声明范围
                        outgoing.setdefault((dep_name, target_text), range_text)
            adjacency[node] = outgoing

    cycle_components: List[Dict[str, Any]] = []
    for component in _tarjan_components(nodes, adjacency):
        members = set(component)
        if len(component) == 1:
            node = component[0]
            # 单节点分量只有存在自环（自依赖）时才算循环
            if node not in adjacency[node]:
                continue
        ordered = sorted(component, key=_node_sort_key)
        edges: List[Dict[str, Any]] = []
        for source in ordered:
            for target, range_text in adjacency[source].items():
                if target in members:
                    edges.append(
                        {
                            "source": {"name": source[0], "version": source[1]},
                            "target": {"name": target[0], "version": target[1]},
                            "version_range": range_text,
                        }
                    )
        edges.sort(key=_edge_sort_key)
        cycle_components.append(
            {
                "nodes": [
                    {"name": node_name, "version": version}
                    for node_name, version in ordered
                ],
                "edges": edges,
            }
        )

    cycle_components.sort(
        key=lambda comp: [
            _node_sort_key((node["name"], node["version"])) for node in comp["nodes"]
        ]
    )
    return {"total": len(cycle_components), "components": cycle_components}

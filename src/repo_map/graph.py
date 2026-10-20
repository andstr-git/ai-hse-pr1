"""Approximate call graph.

An edge caller -> callee exists for every call site resolved to a project symbol.
NetworkX is already a dependency:

    nx.shortest_path(graph, source, target)
"""

import networkx as nx

from repo_map.model import CallSite


class CallGraph:
    def __init__(self, graph: nx.DiGraph):
        self.graph = graph

    @classmethod
    def from_call_sites(cls, call_sites: list[CallSite]) -> "CallGraph":
        graph = nx.DiGraph()
        for site in call_sites:
            if site.resolution == "internal" and site.target:
                graph.add_edge(site.scope, site.target)
        return cls(graph)

    def callers(self, qualified_name: str) -> list[str]:
        if qualified_name not in self.graph:
            return []
        return sorted(self.graph.predecessors(qualified_name))

    def callees(self, qualified_name: str) -> list[str]:
        if qualified_name not in self.graph:
            return []
        return sorted(self.graph.successors(qualified_name))

    def shortest_path(self, source: str, target: str) -> list[str] | None:
        try:
            return nx.shortest_path(self.graph, source, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

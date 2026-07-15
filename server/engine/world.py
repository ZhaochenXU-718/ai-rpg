"""Authoritative spatial graph helpers.

Automatic ``world_rules`` movement was a pre-pivot action mechanic.  The
narrative-first engine keeps only the authored graph; later NPC and fact
commits must still prove that movement is adjacent on this graph.
"""

from __future__ import annotations

from collections import deque

from .content import Story


def board_neighbors(story: Story, node_id: str) -> list[str]:
    neighbors: list[str] = []
    for edge in story.world_edges:
        source, target = edge.get("from"), edge.get("to")
        if source == node_id and isinstance(target, str):
            neighbors.append(target)
        if edge.get("bidirectional") and target == node_id and isinstance(source, str):
            neighbors.append(source)
    return list(dict.fromkeys(neighbors))


def next_hop_toward(story: Story, source: str, target: str) -> str | None:
    """Return the first adjacent hop on a shortest authored path."""
    if source == target:
        return source
    queue = deque([(source, None)])
    visited = {source}
    while queue:
        node, first_hop = queue.popleft()
        for neighbor in board_neighbors(story, node):
            if neighbor in visited:
                continue
            hop = neighbor if first_hop is None else first_hop
            if neighbor == target:
                return hop
            visited.add(neighbor)
            queue.append((neighbor, hop))
    return None

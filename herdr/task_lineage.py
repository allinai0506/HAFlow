"""Pure replacement lineage compatibility for durable and legacy task links."""
import re

REPLACEMENT_SUFFIX_RE = re.compile(r"-r(\d+)$")


def lineage_key(task_id):
    """替换谱系键：(谱系根, 序号)。x -> (x, 1)；x-r2 -> (x, 2)。"""
    text = str(task_id or "")
    match = REPLACEMENT_SUFFIX_RE.search(text)
    if match:
        return text[: match.start()], int(match.group(1))
    return text, 1


def lineage_redispatch_candidates(node_tasks):
    """每条替换谱系里"需要补派"的最新一发（没有则不含该谱系）。

    规则：谱系内只要还有任一非 superseded 成员（在跑/已落定），该谱系就
    已有代表，不再补派；只有当整个谱系都已作废时，才取序号最新的一发
    作为补派对象（且它必须还没有替代者）。

    补派必须按谱系去重：历史被作废任务若被反复补派，会随 fix-loop 轮次
    指数放大（2 -> 4 -> 8 个并发重复任务，实测事故见 lessons §61）。
    """
    by_id = {task.get('task_id'): task for task in node_tasks}
    names = {task_id: lineage_key(task_id) for task_id in by_id}
    named = {}
    for task_id, (name, index) in names.items():
        named.setdefault(name, []).append((index, float(by_id[task_id].get('created_at') or 0), task_id))
    links = {task_id: task['supersedes'] for task_id, task in by_id.items()
             if task.get('supersedes') in by_id and task.get('supersedes')}
    for task_id, task in by_id.items():
        if task.get('superseded_by') in by_id and task.get('superseded_by'):
            links.setdefault(task['superseded_by'], task_id)
    inferred = set()
    for members in named.values():
        previous = None
        for index, _, task_id in sorted(members):
            if task_id not in links and previous and names[previous][1] < index:
                links[task_id] = previous
                inferred.add(task_id)
            previous = task_id

    parents = {name: name for name in named}

    def find_root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for child, predecessor in links.items():
        parents[find_root(names[child][0])] = find_root(names[predecessor][0])

    # Explicit links take precedence if a legal arbitrary name contradicts legacy suffix order.
    visited, invalid = set(), set()
    for task_id in by_id:
        path, positions, cursor = [], {}, task_id
        while cursor in by_id and cursor not in visited:
            if cursor in positions:
                cycle = path[positions[cursor]:]
                # Break suffix assumptions at the explicit successor boundary.
                # Traversal order must not decide which historical task survives.
                weak = [member for member in cycle
                        if member in inferred and links[member] not in inferred]
                if weak:
                    for member in weak:
                        del links[member]
                else:
                    invalid.add(find_root(names[cursor][0]))
                break
            positions[cursor] = len(path)
            path.append(cursor)
            cursor = links.get(cursor)
        visited.update(path)

    groups = {}
    for task_id, (name, index) in names.items():
        groups.setdefault(find_root(name), []).append((index, float(by_id[task_id].get('created_at') or 0), by_id[task_id]))
    linked_predecessors = set(links.values())
    ranks, durable_depths = {}, {}
    for task_id in by_id:
        trail, seen, cursor = [], set(), task_id
        while cursor in by_id and cursor not in ranks and cursor not in seen:
            seen.add(cursor)
            trail.append(cursor)
            cursor = links.get(cursor)
        rank = ranks.get(cursor, 0)
        depth = durable_depths.get(cursor, 0)
        for member in reversed(trail):
            rank = max(names[member][1], rank + 1)
            ranks[member] = rank
            depth += int(member in links and member not in inferred)
            durable_depths[member] = depth

    candidates = []
    for name, members in groups.items():
        if name in invalid:
            continue
        if any(
            task.get("status") != "superseded" for _, _, task in members
        ):
            continue
        leaves = [item for item in members if item[2].get('task_id') not in linked_predecessors]
        if not leaves:
            continue
        _, _, head = max(leaves, key=lambda item: (
            ranks[item[2]['task_id']], durable_depths[item[2]['task_id']],
            item[1], item[2]['task_id']))
        if not head.get("superseded_by") and head.get("replacement_pending") is not False:
            candidates.append(head)
    return candidates



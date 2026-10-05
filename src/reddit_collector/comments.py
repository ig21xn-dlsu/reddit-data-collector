"""Phase-2 comment helpers: flatten trees, normalize records.

Works on the live-verified `/api/comments/tree` shape: a `{"data": [...]}`
envelope of Reddit-style `{"kind", "data"}` nodes (`"t1"` comments,
`"more"` collapsed placeholders), with nested replies under
`data.replies.data.children` (`""` when childless).

Raw storage keeps verbatim wrapper nodes (kind included, so `"more"` nodes
survive); processed storage gets normalized flat records carrying explicit
post/parent linkage plus depth/path for hierarchy reconstruction.
The post collector is untouched by everything in this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

logger = logging.getLogger(__name__)

COMMENT_FIELDS = (
    "comment_id",
    "post_id",
    "parent_id",
    "parent_kind",
    "kind",
    "depth",
    "path",
    "subreddit",
    "author",
    "body",
    "score",
    "created_utc",
    "created_iso",
    "permalink",
    "collapsed_children",
    "collapsed_count",
)


class CommentError(Exception):
    """Raised when a comment tree payload or node is unusable."""


def flatten_comment_tree(payload: Any) -> list[tuple[dict[str, Any], int, tuple[str, ...]]]:
    """Flatten a tree payload to (verbatim node, depth, ancestor-id path) tuples.

    Depth-first, parents before children. Non-mapping nodes are skipped.
    Raises CommentError if the envelope is not a {"data": [...]} object.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CommentError(
            "Unexpected comment tree shape: expected {'data': [...]}"
        )
    flat: list[tuple[dict[str, Any], int, tuple[str, ...]]] = []

    def visit(nodes: Any, depth: int, path: tuple[str, ...]) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                logger.warning("Skipping non-object node in comment tree")
                continue
            flat.append((node, depth, path))
            inner = node.get("data")
            if not isinstance(inner, dict):
                continue
            replies = inner.get("replies")
            children: Any = []
            if isinstance(replies, dict):
                children = (replies.get("data") or {}).get("children", [])
            if not isinstance(children, list):
                continue
            node_id = _bare_id(inner.get("id"))
            visit(children, depth + 1, path + ((node_id,) if node_id else ()))

    visit(payload["data"], 0, ())
    return flat


def _bare_id(value: Any) -> str | None:
    """Strip a t1_/t3_ prefix; None/blank stays None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for prefix in ("t1_", "t3_"):
        if text.startswith(prefix):
            return text[len(prefix):] or None
    return text


def _raw_parent_kind(value: Any) -> str | None:
    """Kind from a raw parent_id prefix (t1_ comment, t3_ post)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.startswith("t1_"):
        return "comment"
    if text.startswith("t3_"):
        return "post"
    return "unknown"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _created_iso(created_utc: Any) -> str | None:
    if isinstance(created_utc, bool) or not isinstance(created_utc, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize_comment(
    node: Mapping[str, Any],
    post_id: str,
    depth: int,
    path: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Map one verbatim tree node to the fixed processed schema (no mutation)."""
    if not isinstance(node, Mapping):
        raise CommentError(f"Cannot normalize comment: expected a mapping, got {type(node).__name__}")
    inner = node.get("data")
    if not isinstance(inner, Mapping):
        raise CommentError("Cannot normalize comment: node has no 'data' object")
    kind = node.get("kind")
    raw_id = inner.get("id")
    comment_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else None
    raw_parent = inner.get("parent_id")
    parent_id = _bare_id(raw_parent)
    created_utc = inner.get("created_utc")
    subreddit = inner.get("subreddit")
    subreddit = str(subreddit) if isinstance(subreddit, str) else (None if subreddit is None else str(subreddit))

    collapsed_children: list[str] = []
    collapsed_count: int | None = None
    if kind == "more":
        kids = inner.get("children")
        if isinstance(kids, list):
            collapsed_children = [str(k) for k in kids if str(k).strip()]
        count = inner.get("count")
        collapsed_count = count if isinstance(count, int) and not isinstance(count, bool) else len(collapsed_children)

    permalink = inner.get("permalink")
    permalink = permalink.strip() if isinstance(permalink, str) and permalink.strip() else None
    if permalink is not None and not permalink.startswith("http"):
        permalink = "https://www.reddit.com" + (permalink if permalink.startswith("/") else "/" + permalink)
    if permalink is None and subreddit and post_id and comment_id and kind != "more":
        permalink = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/_/{comment_id}/"

    body = inner.get("body")
    author = inner.get("author")
    return {
        "comment_id": comment_id,
        "post_id": post_id,
        "parent_id": parent_id,
        "parent_kind": _raw_parent_kind(raw_parent) if parent_id != post_id else "post",
        "kind": kind if isinstance(kind, str) else None,
        "depth": depth,
        "path": list(path),
        "subreddit": subreddit,
        "author": str(author) if isinstance(author, str) else (None if author is None else str(author)),
        "body": str(body) if isinstance(body, str) else (None if body is None else str(body)),
        "score": _as_int(inner.get("score")),
        "created_utc": created_utc,
        "created_iso": _created_iso(created_utc),
        "permalink": permalink,
        "collapsed_children": collapsed_children,
        "collapsed_count": collapsed_count,
    }


def iter_flattened(payload: Any) -> Iterator[tuple[dict[str, Any], int, tuple[str, ...]]]:
    """Yield (node, depth, path) for a tree payload. See flatten_comment_tree."""
    yield from flatten_comment_tree(payload)


__all__ = [
    "COMMENT_FIELDS",
    "CommentError",
    "flatten_comment_tree",
    "iter_flattened",
    "normalize_comment",
]

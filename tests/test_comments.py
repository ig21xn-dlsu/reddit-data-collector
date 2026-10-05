"""Tests for comment tree flattening and normalization. No real API calls."""

from __future__ import annotations

import unittest

from reddit_collector.comments import (
    COMMENT_FIELDS,
    CommentError,
    flatten_comment_tree,
    normalize_comment,
)


def _t1(cid, parent, post="t3_post1", body="hello", replies="", **extra):
    inner = {"id": cid, "parent_id": parent, "link_id": post, "author": "u1",
             "body": body, "score": 3, "created_utc": 1700000000,
             "subreddit": "python", "permalink": f"/r/python/comments/post1/x/{cid}/",
             "replies": replies}
    inner.update(extra)
    return {"kind": "t1", "data": inner}


def _more(children, parent, count=None):
    inner = {"children": children, "parent_id": parent}
    if count is not None:
        inner["count"] = count
    return {"kind": "more", "data": inner}


def _listing(children):
    return {"kind": "Listing", "data": {"children": children}}


class TestFlatten(unittest.TestCase):
    def test_nested_tree_order_depth_and_path(self):
        grandchild = _t1("c3", "t1_c2")
        child = _t1("c2", "t1_c1", replies=_listing([grandchild]))
        top = _t1("c1", "t3_post1", replies=_listing([child]))
        other = _t1("c4", "t3_post1")
        flat = flatten_comment_tree({"data": [top, other]})
        self.assertEqual([(n["data"]["id"], d, p) for n, d, p in flat], [
            ("c1", 0, ()),
            ("c2", 1, ("c1",)),
            ("c3", 2, ("c1", "c2")),
            ("c4", 0, ()),
        ])

    def test_more_nodes_preserved_in_place(self):
        flat = flatten_comment_tree({"data": [_t1("c1", "t3_p"), _more(["a", "b"], "t3_p", 2)]})
        self.assertEqual([n["kind"] for n, _, _ in flat], ["t1", "more"])
        self.assertEqual([d for _, d, _ in flat], [0, 0])

    def test_bad_envelope_rejected(self):
        for bad in (None, [], {"data": "x"}, {"nadata": []}):
            with self.assertRaises(CommentError):
                flatten_comment_tree(bad)

    def test_odd_shapes_tolerated(self):
        flat = flatten_comment_tree({"data": ["junk", {"kind": "t1"}, _t1("c1", "t3_p", replies=None)]})
        kinds = [n.get("kind") for n, _, _ in flat]
        self.assertIn("t1", kinds)
        self.assertEqual(len(flat), 2)  # junk skipped, bare-kind node kept


class TestNormalize(unittest.TestCase):
    def test_full_comment_schema_and_links(self):
        record = normalize_comment(_t1("c2", "t1_c1"), post_id="post1", depth=1, path=["c1"])
        self.assertEqual(tuple(record.keys()), COMMENT_FIELDS)
        self.assertEqual(record["comment_id"], "c2")
        self.assertEqual(record["post_id"], "post1")
        self.assertEqual(record["parent_id"], "c1")
        self.assertEqual(record["parent_kind"], "comment")
        self.assertEqual(record["depth"], 1)
        self.assertEqual(record["path"], ["c1"])
        self.assertEqual(record["body"], "hello")
        self.assertEqual(record["score"], 3)
        self.assertEqual(record["created_iso"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(record["collapsed_children"], [])
        self.assertIsNone(record["collapsed_count"])

    def test_top_level_parent_is_post(self):
        record = normalize_comment(_t1("c1", "t3_post1"), post_id="post1", depth=0, path=[])
        self.assertEqual(record["parent_id"], "post1")
        self.assertEqual(record["parent_kind"], "post")

    def test_more_node_marked_and_unresolved(self):
        record = normalize_comment(_more(["a", "b"], "t1_c1", 2), post_id="post1",
                                   depth=2, path=["c1"])
        self.assertEqual(record["kind"], "more")
        self.assertIsNone(record["comment_id"])
        self.assertEqual(record["collapsed_children"], ["a", "b"])
        self.assertEqual(record["collapsed_count"], 2)
        self.assertEqual(record["parent_id"], "c1")
        self.assertEqual(record["parent_kind"], "comment")
        self.assertIsNone(record["body"])

    def test_missing_fields_become_null(self):
        record = normalize_comment({"kind": "t1", "data": {"id": "x"}}, post_id="p",
                                   depth=0, path=[])
        self.assertEqual(record["comment_id"], "x")
        self.assertIsNone(record["parent_id"])
        self.assertIsNone(record["parent_kind"])
        self.assertIsNone(record["permalink"])

    def test_non_mapping_rejected(self):
        with self.assertRaises(CommentError):
            normalize_comment("nope", post_id="p", depth=0, path=[])
        with self.assertRaises(CommentError):
            normalize_comment({"kind": "t1"}, post_id="p", depth=0, path=[])


if __name__ == "__main__":
    unittest.main()

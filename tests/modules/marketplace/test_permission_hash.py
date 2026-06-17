from app.utils.permission_hash import compute_detail_hash


class TestComputeDetailHash:
    def test_stable_for_same_dict_with_different_key_order(self):
        d1 = {"path": "/api/users", "method": "GET", "extra": {"b": 2, "a": 1}}
        d2 = {"extra": {"a": 1, "b": 2}, "method": "GET", "path": "/api/users"}
        h1, c1 = compute_detail_hash(d1)
        h2, c2 = compute_detail_hash(d2)
        assert h1 == h2
        assert c1 == c2

    def test_canonical_form_is_sorted_and_compact(self):
        _, canonical = compute_detail_hash({"b": 1, "a": 2})
        assert canonical == '{"a":2,"b":1}'

    def test_chinese_not_escaped(self):
        _, canonical = compute_detail_hash({"name": "客户管理"})
        assert canonical == '{"name":"客户管理"}'

    def test_distinct_dicts_have_distinct_hashes(self):
        h1, _ = compute_detail_hash({"a": 1})
        h2, _ = compute_detail_hash({"a": 2})
        assert h1 != h2

    def test_returns_64_char_sha256(self):
        h, _ = compute_detail_hash({"a": 1})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_nested_dict_sorted_recursively(self):
        _, c = compute_detail_hash({"outer": {"z": 1, "a": 2}})
        assert c == '{"outer":{"a":2,"z":1}}'

    def test_list_order_preserved(self):
        _, c1 = compute_detail_hash({"ids": [3, 1, 2]})
        _, c2 = compute_detail_hash({"ids": [3, 1, 2]})
        assert c1 == c2  # same list order → same hash
        _, c3 = compute_detail_hash({"ids": [1, 2, 3]})
        assert c1 != c3  # different list order → different hash (lists not sorted)

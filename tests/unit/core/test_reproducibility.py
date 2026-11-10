import pandas as pd

from core.reproducibility import git_sha, hash_config, hash_dataframe


def test_git_sha_returns_a_40_char_hex_string_in_this_repo() -> None:
    sha = git_sha()
    assert sha is not None
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_hash_config_is_order_independent() -> None:
    a = hash_config({"x": 1, "y": 2})
    b = hash_config({"y": 2, "x": 1})
    assert a == b


def test_hash_config_changes_with_content() -> None:
    a = hash_config({"x": 1})
    b = hash_config({"x": 2})
    assert a != b


def test_hash_dataframe_is_deterministic() -> None:
    df = pd.DataFrame({"a": [1, 2, 3]})
    assert hash_dataframe(df) == hash_dataframe(df.copy())


def test_hash_dataframe_changes_with_content() -> None:
    df1 = pd.DataFrame({"a": [1, 2, 3]})
    df2 = pd.DataFrame({"a": [1, 2, 4]})
    assert hash_dataframe(df1) != hash_dataframe(df2)

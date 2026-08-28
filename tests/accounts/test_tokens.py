from gatekeep.accounts.tokens import hash_token, new_token


def test_new_token_unique_and_hash_stable():
    a, b = new_token(), new_token()
    assert a != b and len(a) > 20
    assert hash_token(a) == hash_token(a)
    assert len(hash_token(a)) == 64  # sha256 hex

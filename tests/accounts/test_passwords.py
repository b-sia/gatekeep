from gatekeep.accounts.passwords import hash_password, verify_password


def test_hash_is_salted_and_verifies():
    h1 = hash_password("hunter2")
    h2 = hash_password("hunter2")
    assert h1 != h2  # per-hash salt
    assert verify_password("hunter2", h1)
    assert not verify_password("wrong", h1)

async def test_database_reachable(db_ping):
    assert db_ping == 1

from shazzzbot.perms import allowed


def test_tiers():
    p = {"operate": {"roles": [5]}, "admin": {"users": [9]}}
    assert allowed(p, "view", 1, {5}) and allowed(p, "operate", 1, {5})
    assert not allowed(p, "admin", 1, {5}) and allowed(p, "admin", 9, set())
    assert not allowed({}, "view", 1, set())

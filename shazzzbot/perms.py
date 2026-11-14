"""Command tiers. Deny by default; a higher tier includes the lower ones."""
TIERS = ("view", "operate", "admin")


def allowed(perms: dict[str, dict[str, list[int]]], tier: str, user_id: int, role_ids: set[int]) -> bool:
    for t in TIERS[TIERS.index(tier):]:
        rule = perms.get(t, {})
        if user_id in rule.get("users", []) or role_ids & set(rule.get("roles", [])):
            return True
    return False



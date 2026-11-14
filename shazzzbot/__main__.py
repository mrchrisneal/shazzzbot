import os
import sys

role = os.environ.get("SHAZZZBOT_ROLE", "bot")
if role == "relay":
    from .relay import main
elif role == "bot":
    from .bot import main
else:
    sys.exit(f"SHAZZZBOT_ROLE must be bot or relay, not {role[:32]!r}")
main()

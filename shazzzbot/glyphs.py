"""Status glyphs for Discord output, kept as escapes so source files stay ASCII.

A status column lines up only if every row's slot is an emoji: emoji and monospace text come from different
fonts, so two spaces miss the emoji width by a device-dependent fraction of a pixel. OK defaults to a small square
that is near-invisible on the dark theme. The bare U+25AA (no U+FE0F) renders as one text cell.
"""
OK = "\u25aa\ufe0f"
GREEN = "\U0001F7E2"
WARN = "\U0001F7E1"
CRIT = "\U0001F534"
DOWN = "\u2b07\ufe0f"
UP = "\u2b06\ufe0f"
PLAY = "\u25b6"
STOP = "\u25a0"
PAUSE = "\u23f8"

BRAILLE = "\u2800"

NO_DATA = "\u2840"

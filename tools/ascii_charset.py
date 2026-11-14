"""Character classes for ascii_guard: what is banned, what is allowed, and classify()."""
import unicodedata

# The tables are keyed by CODEPOINT, not by character, so no banned character exists anywhere in this
# file and it can never fail its own check. A test pins that (tests/test_ascii_guard.py).

# Dashes that are not the ASCII hyphen, with the substitution --fix applies. The em dash and the
# horizontal bar become "--" because they stand in for a longer break; the rest are hyphen-width and
# become "-". CONTRIBUTING.md requires "--" to be JOINED to the words, so the fixer eats the spaces around it.
_DASHES = {
    0x2010: "-",    # hyphen
    0x2011: "-",    # non-breaking hyphen
    0x2012: "-",    # figure dash
    0x2013: "-",    # en dash
    0x2014: "--",   # em dash
    0x2015: "--",   # horizontal bar
    0x2043: "-",    # hyphen bullet
    0x2212: "-",    # minus sign
    0x2E3A: "--",   # two-em dash
    0x2E3B: "--",   # three-em dash
    0xFE58: "--",   # small em dash
    0xFE63: "-",    # small hyphen-minus
    0xFF0D: "-",    # fullwidth hyphen-minus
}

_QUOTES = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"',
    0x2032: "'", 0x2033: '"',
    0x2026: "...",
}

# Emoji, pictographs and the joiners that build them. The dingbat and misc-symbol blocks are in because
# the tells this rule exists to catch live there: the warning sign, the check mark, the star.
#
# These ranges are the WHOLE definition -- there is deliberately no "category is Symbol-other" fallback.
# That fallback was tried and it was too broad by 600 characters: it swept up box-drawing and block
# characters and the degree sign. A scanner that flags legitimate characters gets switched off, which is the same
# outcome as not having one. Anything outside these ranges is reported for review and never blocks.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x1F1E6, 0x1F1FF),
    (0x2600, 0x26FF), (0x2700, 0x27BF), (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),
)
_ZWJ = 0x200D

BANNED_EMOJI = "emoji"
BANNED_DASH = "non-ascii-dash"
BANNED_QUOTE = "smart-quote"
BANNED_METADATA = "comment-metadata"
BANNED_INVISIBLE = "invisible"
BANNED_CONFUSABLE = "confusable"
BANNED_SCRIPT = "non-english-letter"
BANNED_UNLISTED = "not-allowlisted"
REVIEW = "non-ascii"
_BANNED = (BANNED_EMOJI, BANNED_DASH, BANNED_QUOTE, BANNED_METADATA, BANNED_INVISIBLE,
           BANNED_CONFUSABLE, BANNED_SCRIPT, BANNED_UNLISTED)

# Characters that CANNOT BE SEEN, or that reorder what a reader sees. This is a security rule, not a
# style one. A bidi override makes source read one way to a human and execute another, which is the
# whole Trojan Source class of attack. A zero-width space inside an identifier is a bug nobody can
# spot in review and nobody can reproduce from a screenshot. A no-break space looks exactly like a
# space and is not one.
#
# Tab, newline and carriage return are the only control characters a source file has business
# holding, so they are the only ones left out. The zero-width joiner is NOT here: it is already
# classified as emoji, because that is what it is doing when it appears, and moving it would change a
# call the emoji rule already gets right.
_INVISIBLE_RANGES = (
    (0x0000, 0x0008), (0x000B, 0x000C), (0x000E, 0x001F), (0x007F, 0x009F),
    (0x00A0, 0x00A0),                       # no-break space
    (0x00AD, 0x00AD),                       # soft hyphen
    (0x061C, 0x061C),                       # arabic letter mark, a bidi control
    (0x180E, 0x180E),                       # mongolian vowel separator
    (0x2000, 0x200A),                       # en quad through hair space: all look like a space
    (0x200B, 0x200C), (0x200E, 0x200F),     # zero-width space/non-joiner, bidi marks
    (0x2028, 0x202E),                       # line/paragraph separator, bidi embedding and override
    (0x205F, 0x205F), (0x3000, 0x3000),     # medium mathematical space, ideographic space
    (0x2060, 0x2064), (0x2066, 0x206F),     # word joiner, invisible operators, bidi isolates
    (0xFEFF, 0xFEFF),                       # byte order mark
    (0xFFF9, 0xFFFB),                       # interlinear annotation
    (0xFFFC, 0xFFFD),                       # object replacement, replacement char: encoding damage
    (0xE0000, 0xE007F),                     # tag characters: invisible, and a steganography carrier
    (0xE0100, 0xE01EF),                     # variation selectors supplement
)

# VISIBLE, but impersonating ASCII. A fullwidth "a" and a mathematical bold "a" both read as "a" to a
# human and are a different identifier to a compiler. Neither has any business in this codebase, and
# unlike the Greek letters in the classifier's maths there is no legitimate case to protect.
_CONFUSABLE_RANGES = (
    (0xFF01, 0xFF5E),                       # fullwidth ASCII forms
    (0x1D400, 0x1D7FF),                     # mathematical alphanumeric symbols
)

# THE ALLOWLIST, AND IT IS AN ALLOWLIST ON PURPOSE. Everything outside ASCII is refused unless it is
# named here.
#
# A DENYLIST FAILS OPEN, and this file is the proof. It already checked emoji, dashes, quotes,
# invisibles and confusables -- five categories, carefully chosen -- and a lone CJK ideograph walked
# straight through the gap, mid-word, in an English comment, on its way to a public repository. Every
# category somebody forgets to enumerate is silently permitted, and Unicode adds more every year.
# Inverted, the same mistake costs one line in this table instead of an escape.
#
# IT IS TRACTABLE BECAUSE THE REAL SET IS TINY. The whole tracked tree holds 29 distinct non-ASCII
# characters. Each entry below was found by measuring, not guessed, and each says what it is for.
_ALLOWED_RANGES = (
    (0x0304, 0x0304),                       # combining macron: x-bar, a mean, in the classifier
    (0x0370, 0x03FF),                       # greek: MATHEMATICAL NOTATION -- sigma, lambda, alpha, beta
    (0x2190, 0x21FF),                       # arrows
    (0x2200, 0x22FF),                       # mathematical operators
    (0x2500, 0x257F),                       # box drawing: the section separators
    (0x2580, 0x259F),                       # block elements: the block art
    (0x25A0, 0x25FF),                       # geometric shapes: the small UI markers
)
# NAMED ONE BY ONE, never as their blocks: Latin-1 and General Punctuation both contain LETTERS, so
# admitting either whole block would reopen the exact hole this exists to close.
_ALLOWED_CHARS = frozenset(
    "\u00a7"      # section sign
    "\u00b7"      # middle dot
    "\u00b0"      # degree sign
    "\u00b1"      # plus-minus sign
    "\u00d7"      # multiplication sign
    "\u00b4"      # acute accent, in the ASCII art
    "\u2022"      # bullet
    "\u2026"      # horizontal ellipsis
    "\u2039"      # single left-pointing angle quotation mark
)


def _allowed_non_ascii(cp):
    """True when this codepoint is explicitly permitted. Nothing else outside ASCII is."""
    return (chr(cp) in _ALLOWED_CHARS
            or any(lo <= cp <= hi for lo, hi in _ALLOWED_RANGES))



def _is_emoji(cp):
    return cp == _ZWJ or any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def classify(ch):
    """The severity class of one character, or None when it is plain ASCII and of no interest. Dashes
    and quotes are matched BEFORE the emoji ranges: several of them sit inside those blocks, and calling
    a curly apostrophe an emoji would send the reader looking for the wrong thing."""
    cp = ord(ch)
    # Checked BEFORE the ASCII early return: most of these are control characters below 128, and the
    # old early return let every one of them through as "plain ASCII, of no interest".
    if any(lo <= cp <= hi for lo, hi in _INVISIBLE_RANGES):
        return BANNED_INVISIBLE
    if cp < 128:
        return None
    if cp in _DASHES:
        return BANNED_DASH
    if cp in _QUOTES:
        return BANNED_QUOTE
    # After the dash table on purpose: the fullwidth hyphen-minus lives in both, and calling it a dash
    # is the more useful answer because --fix knows how to rewrite it.
    if any(lo <= cp <= hi for lo, hi in _CONFUSABLE_RANGES):
        return BANNED_CONFUSABLE
    if _allowed_non_ascii(cp):
        return None                          # named in the allowlist, with a reason
    # Everything from here is refused. The specific kinds exist only to say something more useful than
    # "not permitted" -- a letter is almost always a generator slipping scripts mid-word, and an emoji
    # is almost always decoration, and the two want different advice.
    if unicodedata.category(ch).startswith("L"):
        return BANNED_SCRIPT
    if _is_emoji(cp):
        return BANNED_EMOJI
    return BANNED_UNLISTED

"""
Keyword filter for violation matching.

These are the ONLY keywords the user wants to match. Edit this file to tune.
Each entry is a regex pattern compiled with re.IGNORECASE.

To add a keyword: add a new line to KEYWORD_PATTERNS.
To remove: delete or comment out the line.

The filter searches the AllegedViolation field on each case row.
"""

KEYWORD_PATTERNS = [
    # Fence / fencing
    r"\bfence(s|ing)?\b",
    r"\bchain[- ]?link( fence)?\b",
    r"\bdurafence\b",
    r"\bmetal fence\b",

    # Gates
    r"\bgate(s)?\b",

    # Doors
    r"\bdoor(s)?\b",
    r"\bgarage door(s)?\b",

    # Windows
    r"\bwindow(s)?\b",

    # Electrical
    r"\belectrical\b",
]

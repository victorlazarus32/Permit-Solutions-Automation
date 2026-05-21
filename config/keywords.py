"""
Keyword filter for violation matching.

These are the ONLY keywords the user wants to match. Edit this file to tune.
Each entry is a regex pattern compiled with re.IGNORECASE.

To add a keyword: add a new line to KEYWORD_PATTERNS.
To remove: delete or comment out the line.

The filter searches the AllegedViolation field on each case row.

Trade scope (Allday Fence + Esteban's roofing license + adjacent work):
fences, gates, durafence, doors, garage, windows, pergolas, terraces,
electrical, sheds, and roofing (added 2026-05-21 — Esteban is a licensed
roofing contractor so any roofing violation is in-scope for letter mailing).
The material+structure compound entries (pvc fence, metal gate, wood fence,
etc.) are functionally redundant with the bare fence/gate regexes, but they
make the matched_keywords column self-documenting at review time.
"""

KEYWORD_PATTERNS = [
    # Fence / fencing (any material).
    # NOTE: stem is `fenc`, not `fence` -- the letter `e` is NOT inside `fencing`
    # or `fenced`. `\bfence(s|ing)?\b` would silently miss "illegal fencing".
    r"\bfenc(e|es|ed|ing)\b",
    r"\bchain[- ]?link( fenc(e|es|ed|ing))?\b",
    r"\bdurafence\b",
    r"\bmetal\s+fenc(e|es|ed|ing)\b",
    r"\bpvc\s+fenc(e|es|ed|ing)\b",
    r"\bwood(en)?\s+fenc(e|es|ed|ing)\b",
    r"\baluminum\s+fenc(e|es|ed|ing)\b",

    # Gates (any material)
    r"\bgate(s)?\b",
    r"\bmetal\s+gate(s)?\b",
    r"\bpvc\s+gate(s)?\b",
    r"\bwood(en)?\s+gate(s)?\b",
    r"\baluminum\s+gate(s)?\b",

    # Doors
    r"\bdoor(s)?\b",
    r"\bgarage\s+door(s)?\b",

    # Garage (broad: covers conversions, additions, new garage construction)
    r"\bgarage(s)?\b",

    # Windows
    r"\bwindow(s)?\b",

    # Pergolas
    r"\bpergolas?\b",

    # Terraces
    r"\bterraces?\b",

    # Sheds (detached storage structures, very common in Miami-Dade violations)
    r"\bshed(s)?\b",

    # Electrical
    r"\belectrical\b",

    # Roofing (added 2026-05-21 — Esteban is licensed for roofing).
    # Covers: roof, roofs, roofed, roofing, roofer(s), and re-roof variants
    # (re-roof, reroof, re-roofing, etc.).
    r"\broof(s|ed|ing|er|ers)?\b",
    r"\bre[- ]?roof(s|ed|ing)?\b",
    r"\bshingles?\b",
    r"\bre[- ]?shingl(e|es|ing)\b",
]

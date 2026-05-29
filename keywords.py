"""Keyword list and matching helper for legacy text triggers."""
# Spanish and English keywords
KEYWORDS = ["necesito", "pito", "i need", "whistle", "pedo", "caca", "fart"]

def checkKeywords(text: str) -> bool:
    """Check whether any configured keyword appears in the text.

    Args:
        text: Input text to scan.

    Returns:
        True if any keyword is found; otherwise False.

    Side Effects:
        None. The input is lowercased for comparison.
    """
    text = text.lower()
    return any(keyword in text for keyword in KEYWORDS)

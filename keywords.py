# Spanish and English keywords
KEYWORDS = ["necesito", "pito", "i need", "whistle"]

def check_keywords(text: str) -> bool:
    """
    Checks if any of the keywords are present in the provided text.
    The text is converted to lowercase for comparison.
    """
    text = text.lower()
    return any(keyword in text for keyword in KEYWORDS)

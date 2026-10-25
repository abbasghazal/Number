def mask_phone(phone: object) -> str:
    """Return a diagnostic-safe phone representation for logs."""
    digits = "".join(character for character in str(phone or "") if character.isdigit())
    if len(digits) <= 4:
        return "***"
    prefix = digits[: min(4, len(digits) - 3)]
    suffix = digits[-3:]
    return f"+{prefix}{'*' * max(5, len(digits) - len(prefix) - len(suffix))}{suffix}"

"""Clamp a numeric value into the closed interval [lower, upper]."""


def clamp(value, lower, upper):
    if lower > upper:
        raise ValueError(
            f"invalid interval: lower ({lower!r}) must not exceed upper ({upper!r})"
        )
    if value < lower:
        return lower
    if value > upper:
        return upper
    return value

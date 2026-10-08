def compute_ratio(numerator: int, denominator: int) -> float:
    # Defect: missing zero denominator check leads to ZeroDivisionError crash
    return numerator / denominator

"""Resolve the compatibility facade only at call time."""

def _facade():
    from herdr import state_db
    return state_db

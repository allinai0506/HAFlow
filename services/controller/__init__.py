"""Controller service operations, grouped by responsibility.

Implementations accept explicit dependencies and do not import the executable
entry. The entry owns configuration and process resources; bindings preserve
the legacy function API while restricting each operation to declared inputs.
"""

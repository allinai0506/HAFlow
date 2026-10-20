"""Bind explicit service dependencies to the legacy Controller namespace.

The namespace belongs to one entry instance. Keeping references live preserves
existing configuration overrides and injected transports without copying state
or modifying implementation modules. Implementations can also be called directly
with an object providing only the dependencies they declare.
"""

from functools import wraps
from importlib import import_module
from inspect import signature


class Dependencies:
    """A restricted view of one operation's declared dependencies."""

    __slots__ = ("_values", "_names")

    def __init__(self, values, names):
        object.__setattr__(self, "_values", values)
        object.__setattr__(self, "_names", frozenset(names))

    def __getattr__(self, name):
        if name not in self._names:
            raise AttributeError(f"Undeclared Controller dependency: {name}")
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        if name not in self._names:
            raise AttributeError(f"Undeclared Controller dependency: {name}")
        self._values[name] = value


def _bind_operation(operation, dependencies, module_name):
    @wraps(operation)
    def bound(*args, **kwargs):
        return operation(dependencies, *args, **kwargs)

    parameters = tuple(signature(operation).parameters.values())[1:]
    bound.__signature__ = signature(operation).replace(parameters=parameters)
    bound.__module__ = module_name
    return bound


def install_modules(namespace, module_names):
    """Expose operations with their original signatures and shared identities."""
    for module_name in module_names:
        module = import_module(f"services.controller.{module_name}")
        for name, required in module.DEPENDENCIES.items():
            operation = getattr(module, name)
            dependencies = Dependencies(namespace, required)
            namespace[name] = _bind_operation(operation, dependencies, namespace["__name__"])

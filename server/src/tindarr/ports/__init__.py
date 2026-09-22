"""Interfaces (``Protocol`` classes and value types) the domain and auth talk to.

Ports depend on nothing else in Tindarr. Adapters implement them; only
``tindarr.main`` wires an adapter to a port.
"""

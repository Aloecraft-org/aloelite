# ./manager/engine/__init__.py
# License: Apache-2.0 (see any sibling module for the full notice)
"""
manager.engine — the Python-engine adapter layer.

Everything here binds the manager to THIS implementation of aloelite: held
in-process mounts (direct), FUSE child processes (supervisor), deployment
preflight, and the volume registry. It is the piece a Rust or Kotlin manager
replaces wholesale; manager.api (the HTTP contract) and manager.ui (the
asset bundle) are the pieces it inherits. See manager/README.md.
"""

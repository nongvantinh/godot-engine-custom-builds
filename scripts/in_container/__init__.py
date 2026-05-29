"""In-container build modules — per-platform Python entry points.

The host orchestrator (:mod:`scripts.host_orchestrator`) ``docker run``s one
image per platform and invokes ``python3 -m scripts.in_container.build_<platform>``
inside the container. The modules in this package are the platform entry
points and the shared helpers they call.

Stdlib-only by contract: containers extend ``godot-fedora`` and ship Python 3
plus scons, but no other Python packages. New non-stdlib dependencies must be
added to the Dockerfile chain, not declared at the Python layer.

Layout:

  * ``common`` — shared helpers (scons wrapper, archive extract, swiftly
    install, d3d12 sdk install, mono-glue copy, swappy copy, bin cleanup
    helper).
  * ``build_<platform>`` — one module per build container; each module exposes
    a ``main(argv=None) -> int`` invoked via ``python3 -m``.
"""

from __future__ import annotations

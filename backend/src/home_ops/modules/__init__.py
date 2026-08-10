"""Feature modules.

One package per feature area, each owning its own router, models, and services,
and each mounted by `home_ops.main`. Yuvomi gets a similar shape from
runtime-scanned folders; we get it from convention, since a bundled frontend
rules out drop-in modules. The benefit we want is the same: a feature is one
directory, and it can be switched off without unpicking it from its neighbours.

Phase 0 has exactly one module: `health`.

Two conventions, both learned the hard way:

**The route module is `routes.py`, never `router.py`.** A submodule sharing a
name with something the package exports gets shadowed by it, so
`from home_ops.modules.healthcheck import router` would hand back the `APIRouter`
rather than the module — silently, and it defeats monkeypatching in tests.

**Package `__init__.py` files import nothing.** Importing routes from
`__init__.py` means that touching *any* name in the package drags the whole
router in, and routers import cross-cutting services like `home_ops.audit`,
which in turn import models from these packages. That is a circular import, and
it only fails depending on which module happens to be imported first — so it
surfaces as a mysterious failure in one test file and nowhere else. `main.py`
imports each `routes` module explicitly instead.
"""

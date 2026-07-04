import importlib
import pkgutil

from core.plugin import RobotPlugin


def discover_plugins() -> dict[str, type[RobotPlugin]]:
    """Scan src/plugins/ for packages that export a RobotPlugin subclass."""
    plugins = {}
    package = importlib.import_module("plugins")
    for _importer, modname, ispkg in pkgutil.iter_modules(package.__path__):
        if not ispkg or modname.startswith("_"):
            continue
        mod = importlib.import_module(f"plugins.{modname}")
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if (
                isinstance(obj, type)
                and issubclass(obj, RobotPlugin)
                and obj is not RobotPlugin
            ):
                plugins[modname] = obj
    return plugins

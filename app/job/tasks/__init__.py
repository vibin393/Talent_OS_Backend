import importlib
import pkgutil
from collections.abc import Generator
from types import ModuleType


def import_task_modules(package_name: str, search_path: list[str]) -> Generator[ModuleType, None, None]:
    for _, modname, _ in pkgutil.iter_modules(search_path):
        yield importlib.import_module(f"{package_name}.{modname}")


# Import all *_tasks modules in this package dynamically
for _module in import_task_modules(__name__, list(__path__)):
    pass  # Module imported and registered

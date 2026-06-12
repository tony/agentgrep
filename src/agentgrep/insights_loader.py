"""Typed lazy loading for optional insights backends."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import importlib
import types
import typing as t

__all__ = [
    "BackendConfigurationError",
    "BackendLoadError",
    "BackendPolicy",
    "BackendUnavailable",
    "ImportModule",
    "LoadedBackend",
    "load_backend_modules",
]

ImportModule = cabc.Callable[[str], types.ModuleType]
PromptPolicy = t.Literal["never", "interactive"]


@dataclasses.dataclass(frozen=True, slots=True)
class BackendPolicy:
    """Runtime policy for optional backend loading and model access."""

    allow_download: bool = False
    allow_network: bool = False
    prompt_policy: PromptPolicy = "never"


@dataclasses.dataclass(frozen=True, slots=True)
class LoadedBackend:
    """Loaded optional modules for one insights backend."""

    level: str
    modules: cabc.Mapping[str, types.ModuleType]

    def require(self, name: str) -> types.ModuleType:
        """Return one loaded module by import path."""
        return self.modules[name]


class BackendUnavailable(RuntimeError):
    """Raised when one or more optional backend modules are unavailable."""

    def __init__(self, level: str, missing_modules: cabc.Iterable[str]) -> None:
        self.level = level
        self.missing_modules = tuple(missing_modules)
        super().__init__(self._format_message())

    @property
    def setup_command(self) -> str | None:
        """Return the setup command for installable optional levels."""
        if self.level in {"html", "ml", "embeddings", "index", "llm"}:
            return f"agentgrep insights setup {self.level} --install --yes"
        return None

    def _format_message(self) -> str:
        missing = ", ".join(self.missing_modules) or "unknown modules"
        message = f"Missing optional insights backend for level {self.level!r}: {missing}."
        if self.setup_command is not None:
            message += f" Run: {self.setup_command}"
        return message


class BackendConfigurationError(RuntimeError):
    """Raised when an installed optional backend needs runtime configuration."""

    def __init__(
        self,
        level: str,
        *,
        requirement: str,
        examples: cabc.Iterable[str] = (),
    ) -> None:
        self.level = level
        self.requirement = requirement
        self.examples = tuple(examples)
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        message = (
            f"Insights backend {self.level!r} needs runtime configuration: {self.requirement}."
        )
        if self.examples:
            examples = "\n".join(f"  {example}" for example in self.examples)
            message += f"\nTry:\n{examples}"
        return message


class BackendLoadError(RuntimeError):
    """Raised when an installed optional backend fails during import."""

    def __init__(self, level: str, module: str, cause: BaseException) -> None:
        self.level = level
        self.module = module
        self.__cause__ = cause
        super().__init__(
            f"Optional insights backend {level!r} failed while importing {module!r}: {cause}",
        )


def load_backend_modules(
    level: str,
    modules: cabc.Iterable[str],
    *,
    import_module: ImportModule | None = None,
) -> LoadedBackend:
    """Import optional backend modules lazily and return them by import path."""
    importer = import_module or importlib.import_module
    loaded: dict[str, types.ModuleType] = {}
    missing: list[str] = []
    for module_name in modules:
        try:
            loaded[module_name] = importer(module_name)
        except ModuleNotFoundError as exc:
            if _is_missing_requested_module(module_name, exc):
                missing.append(module_name)
            else:
                raise BackendLoadError(level, module_name, exc) from exc
        except Exception as exc:
            raise BackendLoadError(level, module_name, exc) from exc
    if missing:
        raise BackendUnavailable(level, missing)
    return LoadedBackend(level=level, modules=loaded)


def _is_missing_requested_module(module_name: str, exc: ModuleNotFoundError) -> bool:
    missing_name = exc.name
    if missing_name is None:
        return True
    return (
        module_name == missing_name
        or module_name.startswith(f"{missing_name}.")
        or missing_name.startswith(f"{module_name}.")
    )

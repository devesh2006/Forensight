"""Small parser registry shared by evidence-ingestion adapters."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any


@dataclass(frozen=True)
class ParserSpec:
    name: str
    extensions: tuple[str, ...]
    mime_types: tuple[str, ...]
    parse: Callable[[str, str, bytes], Any]
    validate: Callable[[bytes], None] | None = None
    detect: Callable[[str, bytes], bool] | None = None


class ParserRegistry:
    def __init__(self):
        self._parsers: dict[str, ParserSpec] = {}

    def register(self, spec: ParserSpec):
        for extension in spec.extensions:
            ext=extension.casefold()
            if ext in self._parsers:raise ValueError(f"A parser is already registered for {ext}")
            self._parsers[ext]=spec

    def supported_extensions(self):
        return frozenset(self._parsers)

    def find(self, filename: str, raw: bytes):
        spec=self._parsers.get(Path(filename).suffix.casefold())
        if not spec:return None
        if spec.detect and not spec.detect(filename,raw):return None
        if spec.validate:spec.validate(raw)
        return spec

    def parse_file(self, display_name: str, filename: str, raw: bytes):
        spec=self.find(filename,raw)
        if not spec:raise ValueError(f"No parser detected for {Path(filename).suffix or 'file'}")
        return spec.parse(display_name,filename,raw)

from pathlib import Path
from support_agent.errors import Permanent


class LocalStorage:
    def __init__(self, root, max_bytes):
        self.root, self.max_bytes = Path(root).resolve(), max_bytes

    def path(self, ref):
        path = (self.root / ref).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise Permanent()
        return path

    def read(self, ref):
        p = self.path(ref)
        if p.stat().st_size > self.max_bytes:
            raise Permanent()
        with p.open("rb") as f:
            b = f.read(self.max_bytes + 1)
        if len(b) > self.max_bytes:
            raise Permanent()
        return b

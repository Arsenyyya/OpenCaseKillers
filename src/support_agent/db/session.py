from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


class Database:
    def __init__(self, url):
        self.engine = create_engine(
            url, pool_size=4, max_overflow=2, pool_pre_ping=True, connect_args={"connect_timeout": 5}
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def tx(self, products=(), actor="service"):
        with self.sessions.begin() as s:
            s.execute(
                text("SELECT set_config('app.products', :p, true), set_config('app.actor', :a, true)"),
                {"p": ",".join(products), "a": actor},
            )
            yield s

    def runtime_ready(self):
        with self.tx() as s:
            r = s.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one()
            owner = s.scalar(text("SELECT tableowner=current_user FROM pg_tables WHERE tablename='tickets'"))
            if r[0] or r[1] or owner:
                raise RuntimeError("runtime database role must be non-owner without superuser/BYPASSRLS")
            s.execute(text("SELECT 1 FROM jobs LIMIT 1"))

    def close(self):
        self.engine.dispose()

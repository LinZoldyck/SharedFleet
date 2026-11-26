from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config

config.DATA_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")  # lectures/écritures concurrentes (web + tâches de fond)
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    add_missing_columns()


def _sql_default(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def add_missing_columns() -> list[str]:
    """Migration légère : ajoute aux tables existantes les colonnes apparues dans models.py.

    create_all() crée les tables manquantes mais ne modifie jamais une table existante. Ici, chaque colonne absente
    est ajoutée avec sa valeur par défaut (ALTER TABLE … ADD COLUMN) : les données existantes ne sont pas touchées.
    Retourne la liste des colonnes ajoutées.
    """
    from sqlalchemy import inspect, text

    added = []
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}'
                default = column.default.arg if column.default is not None and column.default.is_scalar else None
                if default is not None:
                    ddl += f" NOT NULL DEFAULT {_sql_default(default)}" if not column.nullable else f" DEFAULT {_sql_default(default)}"
                elif not column.nullable:
                    raise RuntimeError(f"Colonne {table.name}.{column.name} : une valeur par défaut est nécessaire pour la migration.")
                conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    if added:
        import logging
        logging.getLogger("sharedfleet.db").warning("Base mise à jour : colonnes ajoutées %s", ", ".join(added))
    return added

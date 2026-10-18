import os
import sqlite3
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base

# Fallback database URL
DB_PATH = os.path.join(os.path.dirname(__file__), "rapidaid.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

# Handle PostgreSQL legacy scheme prefix if present
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Convert prisma sqlite syntax if necessary
if DATABASE_URL.startswith("sqlite:///"):
    sqlite_filepath = DATABASE_URL.replace("sqlite:///", "")
    engine = create_engine(f"sqlite:///{sqlite_filepath}", connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

Base = declarative_base()
Base.query = db_session.query_property()

def init_db():
    import models
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)
    try:
        with engine.connect() as conn:
            for col, col_type in [("pincode", "VARCHAR(20)"), ("city", "VARCHAR(100)"), ("taluka", "VARCHAR(100)"), ("district", "VARCHAR(100)")]:
                try:
                    conn.execute(text(f"ALTER TABLE hospitals ADD COLUMN {col} {col_type}"))
                    conn.commit()
                except Exception:
                    pass
    except Exception:
        pass


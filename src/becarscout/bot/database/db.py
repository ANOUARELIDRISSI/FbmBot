from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from becarscout.bot.database.models import Base

DATABASE_URL = "sqlite:///data/bot.db"

# Create the database engine and session factory
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Creates the database schema using SQLAlchemy (idempotent)."""
    Base.metadata.create_all(bind=engine)


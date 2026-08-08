import os
import logging
from dotenv import load_dotenv
from supabase import create_client, Client
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rakoon_backend")

# Load environment variables
load_dotenv(override=True)

# ======================================================================================
# SUPABASE CLIENT SETUP (For transition and legacy support if needed)
# ======================================================================================
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_KEY")

class MockResponse:
    def __init__(self, data):
        self.data = data

class MockQueryBuilder:
    def __init__(self, table_name, db_store):
        self.table_name = table_name
        self.db_store = db_store
        self.filters = []
        self.order_by = None
        self.order_desc = False
        self.insert_data = None

    def insert(self, data):
        self.insert_data = data
        return self

    def select(self, columns="*"):
        return self

    def eq(self, column, value):
        self.filters.append((column, value, "eq"))
        return self

    def ilike(self, column, value):
        self.filters.append((column, value, "ilike"))
        return self

    def order(self, column, desc=False):
        self.order_by = column
        self.order_desc = desc
        return self

    def limit(self, count):
        return self

    def execute(self):
        # Mock database actions
        from datetime import datetime
        if self.insert_data is not None:
            record = dict(self.insert_data)
            if "id" not in record:
                record["id"] = len(self.db_store.get(self.table_name, [])) + 1
            if "timestamp" not in record:
                record["timestamp"] = datetime.utcnow().isoformat()
            if "status_verifikasi" not in record:
                record["status_verifikasi"] = "pending"
            
            if self.table_name not in self.db_store:
                self.db_store[self.table_name] = []
            self.db_store[self.table_name].append(record)
            return MockResponse([record])
        
        records = self.db_store.get(self.table_name, [])
        filtered_records = []
        for r in records:
            match = True
            for col, val, op in self.filters:
                record_val = r.get(col)
                if op == "eq":
                    if str(record_val) != str(val):
                        match = False
                        break
                elif op == "ilike":
                    if record_val is None or val is None:
                        match = False
                        break
                    clean_val = str(val).replace("%", "")
                    if str(record_val).lower() != clean_val.lower():
                        match = False
                        break
            if match:
                filtered_records.append(r)
        
        if self.order_by:
            def sort_key(x):
                return x.get(self.order_by, "")
            filtered_records.sort(key=sort_key, reverse=self.order_desc)
            
        return MockResponse(filtered_records)

class MockSupabaseClient:
    def __init__(self):
        self.db_store = {}

    def table(self, table_name):
        return MockQueryBuilder(table_name, self.db_store)

supabase: Client

if supabase_url and supabase_key:
    try:
        parts = supabase_key.split(".")
        if len(parts) != 3 or not supabase_key.startswith("eyJ"):
            raise ValueError("Invalid JWT key structure")
        supabase = create_client(supabase_url, supabase_key)
        logger.info("Successfully initialized legacy Supabase client.")
    except Exception as e:
        logger.warning(f"Error initializing Supabase client: {str(e)}. Falling back to mock client.")
        supabase = MockSupabaseClient()
else:
    logger.warning("Supabase credentials missing. Falling back to mock client.")
    supabase = MockSupabaseClient()


# ======================================================================================
# SQLALCHEMY ENGINE & SESSION SETUP
# ======================================================================================
DATABASE_URL = os.getenv("DATABASE_URL")

# Fallback to local SQLite if DATABASE_URL is not set in .env
if not DATABASE_URL:
    # default path to rakoon.db in the backend folder
    DATABASE_URL = "sqlite:///./rakoon.db"
    logger.info(f"DATABASE_URL not found in .env. Falling back to local SQLite: {DATABASE_URL}")
else:
    # Ensure correct format for SQLAlchemy for postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    logger.info("DATABASE_URL found. Initializing database engine.")

# Set up engine arguments (connect_args is only for sqlite)
engine_args = {}
if DATABASE_URL.startswith("sqlite"):
    engine_args["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Dependency for FastAPI Routers to inject database sessions
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

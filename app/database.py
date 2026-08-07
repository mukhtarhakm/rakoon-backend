import os
import logging
import uuid
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rakoon_backend")

# Load environment variables
load_dotenv(override=True)

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_KEY")

if not supabase_url:
    raise RuntimeError("Error: SUPABASE_URL environment variable is missing from .env")
if not supabase_key:
    raise RuntimeError("Error: SUPABASE_SERVICE_KEY environment variable is missing from .env")

# ======================================================================================
# KOMENTAR PENTING KEAMANAN:
# SUPABASE_SERVICE_KEY (service_role key) ini HANYA untuk penggunaan backend (server-side).
# Key ini memiliki hak akses penuh (bypass Row Level Security / RLS) ke database.
# JANGAN PERNAH mengirimkan atau menyematkan key ini di sisi client (seperti Flutter/Mobile App)!
# Untuk sisi client, gunakan SUPABASE_ANON_KEY dengan kebijakan RLS yang sesuai.
# ======================================================================================

logger.info(f"Loaded SUPABASE_URL: {supabase_url}")
logger.info(f"Loaded SUPABASE_SERVICE_KEY prefix: {supabase_key[:15]}... (length: {len(supabase_key) if supabase_key else 0})")


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
        self.filters.append((column, value))
        return self

    def order(self, column, desc=False):
        self.order_by = column
        self.order_desc = desc
        return self

    def limit(self, count):
        return self

    def execute(self):
        # Mock INSERT operation
        if self.insert_data is not None:
            record = dict(self.insert_data)
            if "id" not in record:
                # Generate integer ID or UUID string
                record["id"] = len(self.db_store.get(self.table_name, [])) + 1
            if "timestamp" not in record:
                record["timestamp"] = datetime.utcnow().isoformat()
            if "status_verifikasi" not in record:
                record["status_verifikasi"] = "pending"
            
            if self.table_name not in self.db_store:
                self.db_store[self.table_name] = []
            self.db_store[self.table_name].append(record)
            return MockResponse([record])
        
        # Mock SELECT operation
        records = self.db_store.get(self.table_name, [])
        filtered_records = []
        for r in records:
            match = True
            for col, val in self.filters:
                if str(r.get(col)) != str(val):
                    match = False
                    break
            if match:
                filtered_records.append(r)
        
        if self.order_by:
            # Sort helper
            def sort_key(x):
                val = x.get(self.order_by, "")
                # handle datetime comparison strings
                return val
            filtered_records.sort(key=sort_key, reverse=self.order_desc)
            
        return MockResponse(filtered_records)

class MockSupabaseClient:
    def __init__(self):
        self.db_store = {}

    def table(self, table_name):
        return MockQueryBuilder(table_name, self.db_store)

# Global database client placeholder
supabase: Client

try:
    # Basic JWT format check: must have 3 segments separated by dots and start with 'eyJ'
    parts = supabase_key.split(".")
    if len(parts) != 3 or not supabase_key.startswith("eyJ"):
        raise ValueError("Invalid JWT key structure")
        
    supabase = create_client(supabase_url, supabase_key)
    logger.info("Successfully initialized real Supabase client.")
except Exception as e:
    logger.warning(
        "\n"
        "========================================================================\n"
        "WARNING: SUPABASE_SERVICE_KEY in .env is not a valid JWT token!\n"
        "Switching to an in-memory MockSupabaseClient for testing/development.\n"
        "Please configure a valid service_role JWT key in your .env file to connect\n"
        "to your real Supabase database.\n"
        "========================================================================"
    )
    logger.warning(f"Error detail during initialization: {str(e)}")
    supabase = MockSupabaseClient()

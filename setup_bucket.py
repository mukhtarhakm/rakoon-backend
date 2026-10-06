import os
from dotenv import load_dotenv
from supabase import create_client

# Load from .env
load_dotenv(dotenv_path=".env", override=True)

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_KEY")

if not supabase_url or not supabase_key:
    print("[ERROR] SUPABASE_URL or SUPABASE_SERVICE_KEY not found in .env")
    exit(1)

supabase = create_client(supabase_url, supabase_key)
bucket_name = "product-photos"

try:
    buckets = supabase.storage.list_buckets()
    bucket_exists = any(b.name == bucket_name for b in buckets)

    if bucket_exists:
        print("[OK] Bucket '%s' already exists" % bucket_name)
    else:
        print("[INFO] Creating bucket '%s'..." % bucket_name)
        supabase.storage.create_bucket(
            bucket_name,
            options={"public": True}
        )
        print("[OK] Bucket '%s' created successfully (public)" % bucket_name)

except Exception as e:
    print("[ERROR] %s" % str(e))
    exit(1)

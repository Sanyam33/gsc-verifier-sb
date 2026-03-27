import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# Use the Service Role Key for backend operations to bypass RLS if needed
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase credentials missing from environment variables.")

# Initialize the client once
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_supabase() -> Client:
    """
    Dependency to provide the Supabase client to routes.
    """
    return supabase
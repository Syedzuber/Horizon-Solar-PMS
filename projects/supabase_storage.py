import os


def get_supabase_client():
    """
    Build and return an authenticated Supabase client using environment variables.
    Raises ValueError if SUPABASE_URL or SUPABASE_KEY are not set — prevents
    silent failures where uploads appear to succeed but go nowhere.
    The supabase package is imported here (not at module level) so that the app
    can start and run migrations even when the package is not installed.
    """
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be configured")
    # import inside function so missing supabase package raises at call time, not import time
    from supabase import create_client
    return create_client(url, key)

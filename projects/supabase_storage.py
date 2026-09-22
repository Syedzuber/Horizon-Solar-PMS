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


def vendor_order_document_url(doc):
    """
    Public URL for a VendorOrderDocument's file, built from `doc.bucket` and `doc.path`.

    THE SINGLE PLACE TO SWITCH TO SIGNED URLs LATER. VendorOrderDocument stores no URL on
    purpose: every screen that links a PO, PI or invoice must come through here, so the
    day these files move to a private bucket this body changes and nothing else does.

    Built the way raise_payment_request builds its invoice URL — SUPABASE_URL, then
    /storage/v1/object/public/, then bucket and path — except that the path is
    URL-encoded. That one builds it from the raw filename, so a name with a space or a
    '#' produces a broken link; '/' is kept so the path's folders survive.
    """
    from urllib.parse import quote
    from django.conf import settings
    return (
        f"{settings.SUPABASE_URL}/storage/v1/object/public/"
        f"{doc.bucket}/{quote(doc.path, safe='/')}"
    )

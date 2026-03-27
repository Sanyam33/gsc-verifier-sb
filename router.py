import os, httpx, logging
from datetime import datetime, timedelta, timezone
from supabase import Client
from urllib.parse import urlencode, quote
from db import get_supabase
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Request, Query, HTTPException, status
from fastapi.responses import RedirectResponse
from schemas import GSCVerificationCreate, GSCVerificationResult
from typing import List
load_dotenv()
gsc_router = APIRouter(prefix="/api/v1/gsc", tags=["GSC"])
logger = logging.getLogger(__name__)


TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly openid email"
USER_INFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
FRONTEND_URL = os.getenv("FRONTEND_URL")
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

def normalize_site(url: str) -> str:
    return (
        url.replace("https://", "")
           .replace("http://", "")
           .replace("sc-domain:", "")
           .replace("www.", "")
           .rstrip("/")
           .lower()
    )


@gsc_router.post("/request-verification", status_code=status.HTTP_201_CREATED)
def request_gsc_verification(
    data: GSCVerificationCreate, 
    db: Client = Depends(get_supabase)
):
    try:
        # 1. Cleanup old unverified records (Supabase Logic)
        # We calculate the cutoff time in Python
        expiry_limit = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        
        db.table("gsc_verifications") \
            .delete() \
            .eq("verified", False) \
            .lt("created_at", expiry_limit) \
            .execute()

        # 2. Normalize and Prepare Record
        clean_site = normalize_site(str(data.site_url))
        
        insert_data = {
            "site_url": clean_site,
            "verified": False
        }
        
        # 3. Insert into Supabase
        # .execute() returns a response object with .data
        response = db.table("gsc_verifications").insert(insert_data).execute()
        
        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to create verification record")
            
        new_record = response.data[0]
        record_id = new_record["id"]

        # 4. Construct OAuth URL
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": record_id # UUID from Supabase
        }
        
        auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
        return {"auth_url": auth_url, "id": record_id}

    except Exception as e:
        # Supabase client doesn't require manual rollbacks for single inserts
        print(f"Error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize verification."
        )


########################################################


@gsc_router.get("/callback")
async def gsc_callback(request: Request, db: Client = Depends(get_supabase)):
    error = request.query_params.get("error")
    state = request.query_params.get("state")

    if not state:
        raise HTTPException(status_code=400, detail="Missing state")

    # 1. Fetch the record from Supabase
    response = db.table("gsc_verifications").select("*").eq("id", state).maybe_single().execute()
    record = response.data
    
    if not record:
        raise HTTPException(status_code=404, detail="Invalid state")

    status_str = "failed"
    reason = None
    update_data = {} 

    if error:
        reason = error
    else:
        code = request.query_params.get("code")
        if not code:
            reason = "no_code"
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                # Token exchange
                token_res = await client.post(
                    TOKEN_URL,
                    data={
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": REDIRECT_URI,
                    }
                )

                if token_res.status_code != 200:
                    reason = "token_failed"
                else:
                    token_data = token_res.json()
                    access_token = token_data["access_token"]
                    refresh_token = token_data.get("refresh_token")

                    # Get user info
                    user_res = await client.get(
                        USER_INFO_URL,
                        headers={"Authorization": f"Bearer {access_token}"}
                    )
                    user_data = user_res.json() if user_res.status_code == 200 else {}

                    # Get GSC sites
                    sites_res = await client.get(
                        GSC_SITES_URL,
                        headers={"Authorization": f"Bearer {access_token}"}
                    )

                    if sites_res.status_code != 200:
                        reason = "sites_fetch_failed"
                    else:
                        sites_data = sites_res.json()
                        requested_normalized = normalize_site(record["site_url"])
                        verified = False
                        permission_level = None
                        final_site_url = record["site_url"]

                        for site in sites_data.get("siteEntry", []):
                            if normalize_site(site["siteUrl"]) == requested_normalized:
                                permission_level = site["permissionLevel"]
                                if permission_level in ["siteOwner", "siteFullUser"]:
                                    verified = True
                                    final_site_url = site["siteUrl"]
                                break

                        # Prepare the update dictionary
                        update_data = {
                            "verified": verified,
                            "permission_level": permission_level,
                            "access_token": access_token,
                            "site_url": final_site_url,
                            "google_account_id": user_data.get("sub"),
                            "email": user_data.get("email")
                        }

                        if refresh_token:
                            update_data["refresh_token"] = refresh_token

                        # 2. Perform the update in Supabase
                        db.table("gsc_verifications").update(update_data).eq("id", state).execute()
                        
                        status_str = "success" if verified else "unverified"

    current_site = update_data.get("site_url", record["site_url"])
    redirect_url = f"{FRONTEND_URL}?status={status_str}&site={current_site}"
    
    if reason:
        redirect_url += f"&reason={reason}"

    return RedirectResponse(url=redirect_url)

# #############################################################


@gsc_router.get("/verify-result", response_model=GSCVerificationResult)
def get_verification_result(
    site_url: str = Query(..., description="The URL to check verification status for"),
    db: Client = Depends(get_supabase)
):
    clean_site = normalize_site(site_url)

    response = db.table("gsc_verifications") \
        .select("site_url, verified, permission_level") \
        .or_(f'site_url.eq."{site_url}",site_url.eq."{clean_site}"') \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"No verification history found for {site_url}"
        )

    record = response.data[0]
    return record


# ####################################################

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_QUERY_URL = "https://www.googleapis.com/webmasters/v3/sites/{site_url}/searchAnalytics/query"


async def get_access_token(refresh_token: str):
    """Refreshes the Google OAuth token asynchronously."""
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
            resp.raise_for_status() 
            return resp.json()["access_token"]
        except httpx.HTTPStatusError as e:
            error_detail = e.response.json().get("error_description", "Token refresh failed")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error_detail)
        except httpx.RequestError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google Auth service unreachable")

@gsc_router.get("/metrics")
async def get_gsc_metrics(
    site_url: str = Query(...),
    start_date: str = Query(..., example="2026-01-01"),
    end_date: str = Query(..., example="2026-02-01"),
    dimensions: List[str] = Query(["query"], description="e.g. query, page, country, device, date"),
    search_type: str = Query("web", description="web, image, video, news, discover, googleNews"),
    row_limit: int = Query(50, ge=1, le=25000),
    db: Client = Depends(get_supabase)
):

    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date")

    # 1. Database Lookup (Direct Match)
    response = db.table("gsc_verifications") \
        .select("*") \
        .eq("site_url", site_url) \
        .eq("verified", True) \
        .limit(1) \
        .execute()
    
    record = response.data[0] if response.data else None


    if not record:
        clean = normalize_site(site_url)
        # Using ilike to mimic the SQLAlchemy 'contains' logic
        response = db.table("gsc_verifications") \
            .select("*") \
            .ilike("site_url", f"%{clean}%") \
            .eq("verified", True) \
            .limit(1) \
            .execute()
        
        record = response.data[0] if response.data else None

    if not record:
        raise HTTPException(status_code=404, detail="Site not verified or record not found")

    # 2. Asynchronous Token Refresh 
    access_token = await get_access_token(record["refresh_token"])

    # 3. Request Preparation
    final_dimensions = [d for d in dimensions if d != "query"] if search_type in ["discover", "googleNews"] else dimensions

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": final_dimensions,
        "type": search_type,
        "rowLimit": row_limit
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # 4. Asynchronous API Call
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        encoded_site = quote(record["site_url"], safe="")
        url = GSC_QUERY_URL.format(site_url=encoded_site)
        
        try:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()
            
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.json())
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="Search Console API is currently unavailable")
        encoded_site = quote(record.site_url, safe="")
        url = GSC_QUERY_URL.format(site_url=encoded_site)
        
        try:
            resp = await client.post(url, headers=headers, json=body)

            resp.raise_for_status()
            return resp.json()
            
        except httpx.HTTPStatusError as e:
            # Pass the GSC specific error (like 403 permissions) back to the user
            raise HTTPException(status_code=e.response.status_code, detail=e.response.json())
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="Search Console API is currently unavailable")


GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

@gsc_router.delete("/disconnect", status_code=status.HTTP_200_OK)
async def disconnect_gsc_site(
    site_url: str = Query(...),
    db: Client = Depends(get_supabase)
):
    # 1. Database Lookup using the safe limit(1) pattern
    response = db.table("gsc_verifications") \
        .select("*") \
        .eq("site_url", site_url) \
        .eq("verified", True) \
        .limit(1) \
        .execute()

    if not response.data:
        return {"message": "Site was not connected or already removed."}

    record = response.data[0]
    
    # 2. Token Revocation Logic
    token_to_revoke = record.get("refresh_token") or record.get("access_token")
    
    if token_to_revoke:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            try:
                await client.post(
                    f"{GOOGLE_REVOKE_URL}?token={token_to_revoke}",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
            except Exception as e:
                print(f"Token revocation failed (already revoked?): {e}")

    # 3. Delete from Supabase
    try:
        db.table("gsc_verifications") \
          .delete() \
          .eq("id", record["id"]) \
          .execute()
          
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error during disconnection."
        )

    return {
        "status": "success",
        "message": f"Successfully disconnected {site_url} and revoked access tokens."
    }
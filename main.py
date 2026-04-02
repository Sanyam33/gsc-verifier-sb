from fastapi import FastAPI
from router import gsc_router
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()


origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(gsc_router)

@app.get("/")
def root():
    return {"message": "Welcome to the GSC API - Supabase Edition"}


@app.get("/help")
def help():
    return{
        "endpoints": {
            "request-verification": "POST /api/v1/gsc/request-verification",
            "callback": "GET /api/v1/gsc/callback",
            "verify-result": "GET /api/v1/gsc/verify-result",
            "metrics": "GET /api/v1/gsc/metrics",
            "disconnect": "DELETE /api/v1/gsc/disconnect"
        },
        "example_payload": {
            "site_url": "https://example.com/"
        },
        "example_payload_metrics": {
            "site_url": "https://example.com/",
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "dimensions": ["query", "page","country", "device", "date"],
            "search_type": "web",
            "row_limit": "50 (1-25000)"
        }
    }

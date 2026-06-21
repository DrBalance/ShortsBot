"""
main.py
FastAPI 앱 + 수동 트리거 API.
스케줄러는 별도 프로세스(collector/scheduler.py)로 실행.
이 서버는 대시보드 및 수동 실행용 REST API를 제공합니다.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config import config
from db import supabase_client as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = config.validate()
    if missing:
        logger.warning(f"누락된 환경변수: {missing}")
    yield


app = FastAPI(
    title="K뷰티 쇼츠 자동화",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://your-dashboard.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── 헬스체크 ─────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


# ─── 대시보드 통계 ─────────────────────────────────────────


@app.get("/api/stats")
def get_stats():
    """대시보드 메인 통계."""
    try:
        return db.get_dashboard_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/candidates")
def get_candidates(status: str = "pending", limit: int = 20):
    """콘텐츠 후보 목록."""
    try:
        from supabase import create_client
        client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
        result = (
            client.table("kbeauty_content_candidates")
            .select("*")
            .eq("status", status)
            .order("relevance_score", desc=True)
            .limit(limit)
            .execute()
        )
        return {"items": result.data or [], "total": len(result.data or [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/videos")
def get_videos(limit: int = 20):
    """제작된 영상 목록."""
    try:
        from supabase import create_client
        client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
        result = (
            client.table("kbeauty_videos")
            .select("*, kbeauty_content_candidates(shorts_title, trend_topic)")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return {"items": result.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── 수동 트리거 ───────────────────────────────────────────


@app.post("/api/trigger/collect")
async def trigger_collect():
    """수집 봇 수동 실행."""
    from collector.apify_scraper import run_collection
    try:
        count = run_collection()
        return {"success": True, "saved": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/trigger/analyze")
async def trigger_analyze():
    """분석 봇 수동 실행."""
    from collector.claude_analyzer import run_analysis
    try:
        count = run_analysis()
        return {"success": True, "candidates": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/trigger/produce")
async def trigger_produce():
    """제작 봇 수동 실행 (2단계 완성 후 활성화)."""
    return {"success": False, "message": "2단계(영상 제작 봇) 개발 후 활성화됩니다."}


@app.post("/api/trigger/upload")
async def trigger_upload():
    """업로드 봇 수동 실행 (3단계 완성 후 활성화)."""
    return {"success": False, "message": "3단계(유튜브 업로드 봇) 개발 후 활성화됩니다."}


if __name__ == "__main__":
    import uvicorn
    os.makedirs("logs", exist_ok=True)
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

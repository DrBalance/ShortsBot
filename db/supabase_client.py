"""
db/supabase_client.py
Supabase와의 모든 DB 작업을 처리합니다.
"""
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from supabase import create_client, Client
from config import config

logger = logging.getLogger(__name__)


def get_client() -> Client:
    """Supabase 클라이언트 반환."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        raise RuntimeError("Supabase 환경변수가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)


# ─── 원본 게시물 ─────────────────────────────────────────────


def upsert_raw_post(post: dict) -> Optional[dict]:
    """
    인스타 게시물을 저장합니다. 이미 수집된 게시물은 무시합니다.
    
    Args:
        post: {instagram_id, hashtag, caption, likes_count, comments_count,
               image_urls, posted_at}
    Returns:
        저장된 row 또는 None (중복)
    """
    client = get_client()
    try:
        result = (
            client.table("kbeauty_raw_posts")
            .upsert(post, on_conflict="instagram_id", ignore_duplicates=True)
            .execute()
        )
        if result.data:
            logger.info(f"게시물 저장: {post['instagram_id']}")
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"게시물 저장 실패 ({post.get('instagram_id')}): {e}")
        return None


def get_unprocessed_posts(limit: int = 20) -> list[dict]:
    """Claude 분석이 안 된 게시물 조회."""
    client = get_client()
    result = (
        client.table("kbeauty_raw_posts")
        .select("*")
        .eq("is_processed", False)
        .order("likes_count", desc=True)   # 좋아요 많은 순
        .limit(limit)
        .execute()
    )
    return result.data or []


def mark_post_processed(post_id: str) -> None:
    """게시물을 분석 완료로 표시."""
    client = get_client()
    client.table("kbeauty_raw_posts").update(
        {"is_processed": True}
    ).eq("id", post_id).execute()


# ─── 콘텐츠 후보 ─────────────────────────────────────────────


def insert_candidate(candidate: dict) -> Optional[dict]:
    """
    Claude 분석 결과를 콘텐츠 후보로 저장.
    
    Args:
        candidate: {raw_post_id, trend_topic, products, keywords,
                    relevance_score, shorts_title, shorts_script, hook_line}
    """
    client = get_client()
    try:
        result = (
            client.table("kbeauty_content_candidates")
            .insert(candidate)
            .execute()
        )
        logger.info(f"후보 저장: {candidate.get('trend_topic')}")
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"후보 저장 실패: {e}")
        return None


def get_pending_candidates(limit: int = 5) -> list[dict]:
    """제작 대기 중인 후보 목록 조회 (관련도 높은 순)."""
    client = get_client()
    result = (
        client.table("kbeauty_content_candidates")
        .select("*, kbeauty_raw_posts(image_urls)")
        .eq("status", "pending")
        .order("relevance_score", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def update_candidate_status(candidate_id: str, status: str) -> None:
    """후보 상태 업데이트."""
    client = get_client()
    client.table("kbeauty_content_candidates").update(
        {"status": status}
    ).eq("id", candidate_id).execute()


# ─── 영상 ────────────────────────────────────────────────────


def insert_video(video: dict) -> Optional[dict]:
    """제작 완료된 영상 정보 저장."""
    client = get_client()
    try:
        result = (
            client.table("kbeauty_videos")
            .insert(video)
            .execute()
        )
        logger.info(f"영상 저장: {video.get('video_r2_key')}")
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"영상 저장 실패: {e}")
        return None


def get_unuploaded_videos(limit: int = 3) -> list[dict]:
    """업로드 안 된 영상 조회."""
    client = get_client()
    result = (
        client.table("kbeauty_videos")
        .select("*, kbeauty_content_candidates(shorts_title, keywords)")
        .is_("youtube_video_id", "null")
        .order("created_at")
        .limit(limit)
        .execute()
    )
    return result.data or []


def update_video_youtube_info(video_id: str, youtube_data: dict) -> None:
    """유튜브 업로드 완료 후 정보 업데이트."""
    client = get_client()
    client.table("kbeauty_videos").update({
        "youtube_video_id": youtube_data["video_id"],
        "youtube_url": youtube_data["url"],
        "youtube_title": youtube_data.get("title", {}),
        "uploaded_at": datetime.utcnow().isoformat(),
    }).eq("id", video_id).execute()


def update_video_analytics(video_id: str, analytics: dict) -> None:
    """조회수/CTR 등 성과 지표 업데이트."""
    client = get_client()
    client.table("kbeauty_videos").update({
        "views": analytics.get("views", 0),
        "likes": analytics.get("likes", 0),
        "ctr": analytics.get("ctr", 0.0),
        "watch_time_avg": analytics.get("watch_time_avg", 0.0),
    }).eq("id", video_id).execute()


# ─── 대시보드용 통계 ──────────────────────────────────────────


def get_dashboard_stats() -> dict:
    """대시보드 메인 통계."""
    client = get_client()

    raw_count = client.table("kbeauty_raw_posts").select("id", count="exact").execute()
    candidate_counts = (
        client.table("kbeauty_content_candidates")
        .select("status", count="exact")
        .execute()
    )
    video_count = client.table("kbeauty_videos").select("id", count="exact").execute()
    uploaded_count = (
        client.table("kbeauty_videos")
        .select("id", count="exact")
        .not_.is_("youtube_video_id", "null")
        .execute()
    )

    return {
        "raw_posts": raw_count.count or 0,
        "candidates": {row["status"]: row for row in (candidate_counts.data or [])},
        "total_videos": video_count.count or 0,
        "uploaded_videos": uploaded_count.count or 0,
    }

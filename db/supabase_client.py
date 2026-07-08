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
        .order("likes_count", desc=True)
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
                    relevance_score, shorts_title, shorts_script, hook_line,
                    content_type, consumer_problem, consumer_expectation,
                    scenes, prdt_no, product_image_url, scene_image_url}
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


def get_candidate(candidate_id: str) -> Optional[dict]:
    """
    단일 candidate 조회.

    Args:
        candidate_id: kbeauty_content_candidates.id (UUID 문자열)
    Returns:
        candidate dict 또는 None
    """
    client = get_client()
    try:
        result = (
            client.table("kbeauty_content_candidates")
            .select("*")
            .eq("id", candidate_id)
            .single()
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"candidate 조회 실패 ({candidate_id}): {e}")
        return None


def get_candidates_by_status(status: str, limit: int = 5) -> list[dict]:
    """
    특정 status의 candidate 목록 조회.

    Args:
        status: 'pending' | 'video_ready' | 'generation_failed' 등
        limit: 최대 조회 수
    """
    client = get_client()
    result = (
        client.table("kbeauty_content_candidates")
        .select("*")
        .eq("status", status)
        .order("relevance_score", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def get_pending_candidates(limit: int = 5) -> list[dict]:
    """제작 대기 중인 후보 목록 조회 (관련도 높은 순). 하위 호환용."""
    return get_candidates_by_status("pending", limit=limit)


def update_candidate_status(
    candidate_id: str,
    status: str,
    extra: Optional[dict] = None,
) -> None:
    """
    후보 상태 업데이트. 추가 필드도 함께 업데이트 가능.

    Args:
        candidate_id: kbeauty_content_candidates.id
        status: 변경할 status 값
        extra: 함께 업데이트할 추가 필드 dict
               예: {"video_url": "...", "video_engine": "kling"}
    """
    client = get_client()
    payload = {"status": status}
    if extra:
        payload.update(extra)
    client.table("kbeauty_content_candidates").update(
        payload
    ).eq("id", candidate_id).execute()
    logger.info(f"candidate 상태 업데이트: {candidate_id} → {status}")


def update_candidate_scene_image(
    candidate_id: str,
    scene_image_url: str,
) -> None:
    """
    ChatGPT로 생성한 9:16 배경 합성 이미지 URL 저장.
    큐레이터가 이미지 생성 후 수동으로 호출.
    product_image_url까지 다 채웠으면 mark_candidate_curated()를 이어서 호출할 것.

    Args:
        candidate_id: kbeauty_content_candidates.id
        scene_image_url: R2에 업로드된 9:16 이미지 URL
    """
    client = get_client()
    client.table("kbeauty_content_candidates").update(
        {"scene_image_url": scene_image_url}
    ).eq("id", candidate_id).execute()
    logger.info(f"scene_image_url 저장: {candidate_id}")


def update_candidate_product_info(
    candidate_id: str,
    prdt_no: str,
    product_image_url: str,
) -> None:
    """
    Olive Young 제품 정보(prdtNo + og:image URL) 저장.
    큐레이터가 큐레이터 링크 생성 후 수동으로 호출.
    scene_image_url까지 다 채웠으면 mark_candidate_curated()를 이어서 호출할 것.

    Args:
        candidate_id: kbeauty_content_candidates.id
        prdt_no: Olive Young 제품 번호 (예: "GA210004538")
        product_image_url: Olive Young CDN 이미지 URL
    """
    client = get_client()
    client.table("kbeauty_content_candidates").update({
        "prdt_no": prdt_no,
        "product_image_url": product_image_url,
    }).eq("id", candidate_id).execute()
    logger.info(f"제품 정보 저장: {candidate_id}, prdtNo={prdt_no}")


def mark_candidate_curated(candidate_id: str) -> None:
    """
    큐레이터가 product_image_url + scene_image_url을 모두 채운 뒤 호출.
    production 파이프라인(producer/pipeline.py)이 처리 대상을 찾을 때
    이 status를 기준으로 조회하므로, 필드만 채우고 이 함수를 호출하지 않으면
    영상 제작이 시작되지 않는다.

    Args:
        candidate_id: kbeauty_content_candidates.id

    Raises:
        ValueError: product_image_url 또는 scene_image_url이 비어있는 경우
    """
    candidate = get_candidate(candidate_id)
    if not candidate:
        raise ValueError(f"candidate {candidate_id}를 찾을 수 없습니다.")
    if not candidate.get("product_image_url"):
        raise ValueError(f"candidate {candidate_id}: product_image_url이 없습니다.")
    if not candidate.get("scene_image_url"):
        raise ValueError(f"candidate {candidate_id}: scene_image_url이 없습니다.")

    update_candidate_status(candidate_id, "curated")
    logger.info(f"candidate 큐레이션 완료 처리: {candidate_id}")


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

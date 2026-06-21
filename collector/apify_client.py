"""
Apify Instagram Hashtag Scraper 연동
"""
import logging
import time
from typing import Generator

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"
# 공식 Instagram Hashtag Scraper actor ID
ACTOR_ID = "reGe1ST3OBgYZSsZJ"


class ApifyClient:
    def __init__(self, token: str | None = None):
        self.token = token or settings.APIFY_API_TOKEN
        if not self.token:
            raise RuntimeError("APIFY_API_TOKEN이 설정되지 않았습니다.")
        self.headers = {"Content-Type": "application/json"}

    # ── Actor 실행 ────────────────────────────────────────────────────────

    def run_actor(self, hashtags: list[str], max_posts: int = 30) -> str:
        """Actor 실행 후 run_id 반환"""
        url = f"{APIFY_BASE}/acts/{ACTOR_ID}/runs?token={self.token}"
        payload = {
            "hashtags": hashtags,
            "resultsLimit": max_posts,
            "scrapePostsUntilDate": None,     # 최신 포스트만
        }
        with httpx.Client(timeout=30) as client:
            res = client.post(url, json=payload, headers=self.headers)
            res.raise_for_status()
        run_id = res.json()["data"]["id"]
        logger.info(f"[Apify] Actor 실행 시작: run_id={run_id}, tags={hashtags}")
        return run_id

    # ── 완료 대기 ─────────────────────────────────────────────────────────

    def wait_for_finish(self, run_id: str, poll_sec: int = 10, timeout_sec: int = 300) -> str:
        """
        Actor 실행 완료까지 폴링.
        반환값: 'SUCCEEDED' | 'FAILED' | 'TIMED-OUT' | ...
        """
        url = f"{APIFY_BASE}/actor-runs/{run_id}?token={self.token}"
        elapsed = 0
        while elapsed < timeout_sec:
            with httpx.Client(timeout=15) as client:
                res = client.get(url)
                res.raise_for_status()
            status = res.json()["data"]["status"]
            logger.debug(f"[Apify] run_id={run_id} status={status} ({elapsed}s)")
            if status in ("SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"):
                return status
            time.sleep(poll_sec)
            elapsed += poll_sec
        return "TIMED-OUT"

    # ── 결과 조회 ─────────────────────────────────────────────────────────

    def get_results(self, run_id: str) -> list[dict]:
        """완료된 run의 데이터셋 아이템 목록 반환"""
        url = f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items?token={self.token}&limit=1000"
        with httpx.Client(timeout=30) as client:
            res = client.get(url)
            res.raise_for_status()
        items = res.json()
        logger.info(f"[Apify] 수집 결과: {len(items)}건")
        return items

    # ── 편의 메서드: 실행 → 대기 → 결과 한 번에 ──────────────────────────

    def scrape_hashtags(self, hashtags: list[str], max_posts: int = 30) -> list[dict]:
        run_id = self.run_actor(hashtags, max_posts)
        status = self.wait_for_finish(run_id)
        if status != "SUCCEEDED":
            raise RuntimeError(f"[Apify] 스크래핑 실패: status={status}")
        return self.get_results(run_id)


# ── 파싱 유틸 ─────────────────────────────────────────────────────────────

def parse_post(raw: dict) -> dict | None:
    """
    Apify 원본 아이템 → DB 저장용 dict 변환.
    필수 필드 없으면 None 반환 (필터링).
    """
    post_id = raw.get("id") or raw.get("shortCode")
    if not post_id:
        return None

    return {
        "post_id": str(post_id),
        "caption": raw.get("caption", ""),
        "likes_count": raw.get("likesCount") or raw.get("likes", 0),
        "comments_count": raw.get("commentsCount") or raw.get("comments", 0),
        "image_urls": raw.get("images") or (
            [raw["displayUrl"]] if raw.get("displayUrl") else []
        ),
        "post_url": raw.get("url") or f"https://www.instagram.com/p/{raw.get('shortCode', '')}/",
        "posted_at": raw.get("timestamp"),
    }

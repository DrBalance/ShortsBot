"""
collector/apify_scraper.py
Apify Instagram Hashtag Scraper를 호출하여 게시물을 수집합니다.
"""
import logging
import time
from datetime import datetime
from typing import Optional

import httpx
from config import config
from db import supabase_client as db

logger = logging.getLogger(__name__)

APIFY_BASE_URL = "https://api.apify.com/v2"


class ApifyScraper:
    def __init__(self):
        self.token = config.APIFY_API_TOKEN
        self.actor_id = config.APIFY_ACTOR_ID
        self.headers = {"Content-Type": "application/json"}

    # ─── Actor 실행 ───────────────────────────────────────────

    def _run_actor(self, hashtags: list[str]) -> Optional[str]:
        """
        Apify Actor를 실행하고 run_id를 반환합니다.
        """

        hashtag_urls = [
            f"https://www.instagram.com/explore/tags/{tag.replace('#', '')}/"
            for tag in hashtags
        ]
        payload = {
            "directUrls": hashtag_urls,
            "resultsType": "posts",
            "resultsLimit": config.MAX_POSTS_PER_HASHTAG,
            "proxy": {"useApifyProxy": True},
        }
        
        url = f"{APIFY_BASE_URL}/acts/{self.actor_id}/runs?token={self.token}"

        resp = httpx.post(url, json=payload, headers=self.headers, timeout=30)
        resp.raise_for_status()

        run_id = resp.json()["data"]["id"]
        logger.info(f"Apify Actor 실행 시작: run_id={run_id}")
        return run_id

    def _wait_for_run(self, run_id: str, timeout: int = 300) -> bool:
        """
        Actor 실행이 완료될 때까지 폴링합니다.
        timeout: 최대 대기 시간(초)
        """
        url = f"{APIFY_BASE_URL}/actor-runs/{run_id}?token={self.token}"
        elapsed = 0

        while elapsed < timeout:
            resp = httpx.get(url, timeout=10)
            status = resp.json()["data"]["status"]

            if status == "SUCCEEDED":
                logger.info(f"Actor 완료: run_id={run_id}")
                return True
            elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                logger.error(f"Actor 실패: run_id={run_id}, status={status}")
                return False

            time.sleep(15)
            elapsed += 15

        logger.error(f"Actor 타임아웃: run_id={run_id}")
        return False

    def _fetch_results(self, run_id: str) -> list[dict]:
        """Actor 실행 결과를 가져옵니다."""
        url = f"{APIFY_BASE_URL}/actor-runs/{run_id}/dataset/items?token={self.token}"
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ─── 파싱 & 필터링 ────────────────────────────────────────

    def _parse_post(self, raw: dict, hashtag: str) -> Optional[dict]:

        """
        Apify 결과를 DB 스키마에 맞게 변환합니다.
        좋아요 기준 미달 시 None 반환.
        """
    if not isinstance(raw, dict):
        return None

    likes = raw.get("likesCount") or 0
    if likes < config.MIN_LIKES:
        return None

    # 날짜 파싱
    posted_at = None
    ts = raw.get("timestamp")
    if ts:
        try:
            posted_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass

    # 이미지 URL
    image_urls = []
    if raw.get("displayUrl"):
        image_urls.append(raw["displayUrl"])
    if raw.get("videoUrl"):
        image_urls.append(raw["videoUrl"])
    for img in raw.get("images") or []:
        if isinstance(img, str):
            image_urls.append(img)
        elif isinstance(img, dict) and img.get("src"):
            image_urls.append(img["src"])

    return {
        "instagram_id": raw.get("id") or raw.get("shortCode", ""),
        "hashtag": hashtag,
        "caption": (raw.get("caption") or "")[:2000],
        "likes_count": likes,
        "comments_count": raw.get("commentsCount") or 0,
        "image_urls": image_urls[:5],
        "posted_at": posted_at,
    }
    
    

    # ─── 메인 수집 함수 ───────────────────────────────────────

    def collect(self, hashtags: Optional[list[str]] = None) -> int:
        """
        인스타그램 해시태그 데이터를 수집하여 DB에 저장합니다.
        
        Args:
            hashtags: 수집할 해시태그 목록. None이면 config 기본값 사용.
        Returns:
            저장된 신규 게시물 수
        """
        if not self.token:
            raise RuntimeError("APIFY_API_TOKEN이 설정되지 않았습니다.")

        target_hashtags = hashtags or (config.HASHTAGS_KR + config.HASHTAGS_JP)
        logger.info(f"수집 시작: {target_hashtags}")

        run_id = self._run_actor(target_hashtags)
        if not self._wait_for_run(run_id):
            raise RuntimeError(f"Apify Actor 실행 실패: run_id={run_id}")

        raw_results = self._fetch_results(run_id)
        logger.info(f"수집된 원시 게시물: {len(raw_results)}건")

        saved = 0
        for raw in raw_results:
            # 해시태그 정보가 raw에 있으면 사용, 없으면 첫 번째 해시태그로 대체
            hashtag = raw.get("hashtag") or target_hashtags[0]
            post = self._parse_post(raw, hashtag)
            if post and db.upsert_raw_post(post):
                saved += 1

        logger.info(f"수집 완료: 신규 저장 {saved}건")
        return saved


def run_collection() -> int:
    """스케줄러에서 호출하는 진입점."""
    scraper = ApifyScraper()
    return scraper.collect()

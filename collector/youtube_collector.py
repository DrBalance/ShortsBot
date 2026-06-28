"""
collector/youtube_collector.py
영어권 K뷰티 유튜브 영상을 검색하고 댓글을 수집하여 소구점을 추출합니다.

2단계 구조:
  1. 시드 채널 or 제품명 키워드로 관련 영상 URL 수집 (Apify YouTube Scraper)
  2. 수집된 영상 URL로 댓글 수집 (Apify YouTube Comments Scraper)
  3. Claude로 consumer_problem / consumer_expectation 추출
"""
import json
import logging
import time

import anthropic
import requests

from config import config

logger = logging.getLogger(__name__)

# ─── 시드 채널 목록 ───────────────────────────────────────────────
# K뷰티 스킨케어 특화, 영어권 주류 시청자, 구독자 10만~120만
SEED_CHANNELS = [
    {"name": "Gothamista",   "url": "https://www.youtube.com/@gothamista"},
    {"name": "Glow With Ava","url": "https://www.youtube.com/@GlowWithAva"},
    {"name": "Joan Kim",     "url": "https://www.youtube.com/@joanday"},
    {"name": "Liah Yoo",     "url": "https://www.youtube.com/c/LiahYoo"},
    {"name": "Soo Beauty",   "url": "https://www.youtube.com/c/SooBeauty"},
]

# Apify Actor IDs
YOUTUBE_SCRAPER_ACTOR    = "streamers/youtube-scraper"
COMMENTS_SCRAPER_ACTOR   = "scrapapi/youtube-comments-scraper"

APIFY_BASE_URL = "https://api.apify.com/v2"


# ─── 소구점 추출 프롬프트 ─────────────────────────────────────────

EXTRACT_SYSTEM_PROMPT = """You are a K-beauty consumer insights analyst.
Analyze YouTube comments to extract consumer pain points and expectations.
Respond only in JSON format. No other text.
"""

EXTRACT_USER_TEMPLATE = """Analyze these YouTube comments from a K-beauty video about {product_keyword}.

=== Comments ===
{comments_text}

Extract the real consumer signals from these comments. Focus on:
- Specific skin concerns mentioned (oily skin, acne, dry skin, sensitive skin, dark spots, etc.)
- Questions about product suitability for their skin type/tone
- Comparisons to products they already use
- Concerns about ingredients, texture, or performance
- What outcome they're hoping for

=== Response Format (JSON) ===
{{
  "consumer_problems": [
    "concise description of a specific skin problem or concern mentioned",
    "another problem"
  ],
  "consumer_expectations": [
    "what outcome/benefit they want from this type of product",
    "another expectation"
  ],
  "skin_types_mentioned": ["oily", "dry", "combination", "sensitive", "dark skin tones"],
  "key_questions": [
    "does this work for oily skin?",
    "will this cause white cast on dark skin?"
  ],
  "signal_strength": 0.0
}}

signal_strength: 0.0~1.0. High (0.8+) = many specific, actionable pain points.
Low (below 0.5) = mostly generic praise, skip this.
Only include consumer_problems and consumer_expectations that appear in multiple comments or seem highly representative.
"""


# ─── Apify 헬퍼 ──────────────────────────────────────────────────

def _run_apify_actor(actor_id: str, input_data: dict, timeout_secs: int = 120) -> list[dict]:
    """Apify Actor를 실행하고 결과 데이터셋을 반환합니다."""
    headers = {"Content-Type": "application/json"}
    params  = {"token": config.APIFY_API_TOKEN}

    # Actor 실행
    run_url = f"{APIFY_BASE_URL}/acts/{actor_id}/runs"
    resp = requests.post(run_url, json=input_data, headers=headers, params=params, timeout=30)
    resp.raise_for_status()

    run_id      = resp.json()["data"]["id"]
    dataset_id  = resp.json()["data"]["defaultDatasetId"]
    logger.info(f"Apify run started: {actor_id} / run={run_id}")

    # 완료 대기
    status_url = f"{APIFY_BASE_URL}/actor-runs/{run_id}"
    elapsed = 0
    poll_interval = 5
    while elapsed < timeout_secs:
        time.sleep(poll_interval)
        elapsed += poll_interval
        status_resp = requests.get(status_url, params=params, timeout=10)
        status = status_resp.json()["data"]["status"]
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify Actor failed: {status}")

    if elapsed >= timeout_secs:
        raise TimeoutError(f"Apify Actor timed out after {timeout_secs}s")

    # 결과 수집
    items_url = f"{APIFY_BASE_URL}/datasets/{dataset_id}/items"
    items_resp = requests.get(items_url, params={**params, "format": "json"}, timeout=30)
    items_resp.raise_for_status()
    return items_resp.json()


# ─── 1단계: 영상 URL 수집 ────────────────────────────────────────

def search_videos_by_keyword(product_keyword: str, max_results: int = 5) -> list[str]:
    """
    제품명 키워드로 유튜브 영상을 검색하여 URL 목록을 반환합니다.
    예: "anua toner review" → 상위 5개 영상 URL
    """
    search_query = f"{product_keyword} review korean skincare"
    logger.info(f"YouTube 영상 검색: '{search_query}'")

    input_data = {
        "searchKeywords": search_query,
        "maxResults": max_results,
        "language": "en",
    }

    try:
        items = _run_apify_actor(YOUTUBE_SCRAPER_ACTOR, input_data, timeout_secs=120)
        urls = [item["url"] for item in items if item.get("url")]
        logger.info(f"영상 {len(urls)}개 수집됨")
        return urls
    except Exception as e:
        logger.error(f"영상 검색 실패 ({product_keyword}): {e}")
        return []


def get_seed_channel_videos(channel_url: str, max_results: int = 5) -> list[str]:
    """
    시드 채널에서 최근 영상 URL을 수집합니다.
    K뷰티/올리브영 관련 영상만 필터링합니다.
    """
    logger.info(f"시드 채널 영상 수집: {channel_url}")

    input_data = {
        "startUrls": [{"url": channel_url}],
        "maxResults": max_results * 3,  # 필터링 감안해 여유 있게
    }

    try:
        items = _run_apify_actor(YOUTUBE_SCRAPER_ACTOR, input_data, timeout_secs=120)

        # K뷰티 관련 영상만 필터링
        kbeauty_keywords = [
            "korean", "k-beauty", "kbeauty", "olive young", "skincare",
            "toner", "serum", "sunscreen", "moisturizer", "haul"
        ]
        filtered = []
        for item in items:
            title = (item.get("title") or "").lower()
            if any(kw in title for kw in kbeauty_keywords):
                filtered.append(item["url"])
            if len(filtered) >= max_results:
                break

        logger.info(f"K뷰티 영상 {len(filtered)}개 필터링됨")
        return filtered
    except Exception as e:
        logger.error(f"채널 영상 수집 실패 ({channel_url}): {e}")
        return []


# ─── 2단계: 댓글 수집 ────────────────────────────────────────────

def scrape_comments(video_urls: list[str], max_comments_per_video: int = 100) -> list[dict]:
    """
    영상 URL 목록에서 댓글을 수집합니다.

    Returns:
        [{"video_url": ..., "comment": ..., "likes": ...}, ...]
    """
    if not video_urls:
        return []

    logger.info(f"댓글 수집 시작: {len(video_urls)}개 영상")

    input_data = {
        "videoUrls": video_urls,
        "maxCommentsPerVideo": max_comments_per_video,
        "sortBy": "top",  # 좋아요 많은 댓글 우선
    }

    try:
        items = _run_apify_actor(COMMENTS_SCRAPER_ACTOR, input_data, timeout_secs=180)
        # 영어 댓글만 필터 (기본 ASCII + 영어 비율 체크)
        english_comments = []
        for item in items:
            text = item.get("text") or item.get("comment") or ""
            if text and _is_english(text):
                english_comments.append({
                    "video_url": item.get("videoUrl", ""),
                    "comment":   text,
                    "likes":     item.get("likesCount") or item.get("likes") or 0,
                })
        logger.info(f"영어 댓글 {len(english_comments)}개 수집됨")
        return english_comments
    except Exception as e:
        logger.error(f"댓글 수집 실패: {e}")
        return []


def _is_english(text: str) -> bool:
    """텍스트가 영어인지 간단히 판별합니다 (ASCII 비율 기준)."""
    if not text:
        return False
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars / len(text) > 0.8


# ─── 3단계: Claude로 소구점 추출 ─────────────────────────────────

def extract_pain_points(
    comments: list[dict],
    product_keyword: str,
    client: anthropic.Anthropic,
) -> dict:
    """
    수집된 댓글에서 consumer_problem / consumer_expectation을 추출합니다.

    Returns:
        {
          "consumer_problems": [...],
          "consumer_expectations": [...],
          "skin_types_mentioned": [...],
          "key_questions": [...],
          "signal_strength": 0.0~1.0
        }
    """
    if not comments:
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}

    # 상위 50개 댓글만 사용 (좋아요 순)
    top_comments = sorted(comments, key=lambda x: x["likes"], reverse=True)[:50]
    comments_text = "\n".join(
        f"[{c['likes']} likes] {c['comment']}" for c in top_comments
    )

    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=EXTRACT_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": EXTRACT_USER_TEMPLATE.format(
                    product_keyword=product_keyword,
                    comments_text=comments_text,
                ),
            }],
        )
        result = json.loads(message.content[0].text.strip())
        logger.info(
            f"소구점 추출 완료: problems={len(result.get('consumer_problems', []))}, "
            f"signal={result.get('signal_strength', 0):.2f}"
        )
        return result
    except Exception as e:
        logger.error(f"소구점 추출 실패: {e}")
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}


# ─── 통합 실행 함수 ───────────────────────────────────────────────

def collect_pain_points_for_product(product_keyword: str) -> dict:
    """
    제품 키워드에 대한 소구점을 수집합니다.
    claude_analyzer.py의 분류 단계에서 호출합니다.

    Args:
        product_keyword: 예) "anua toner", "beauty of joseon sunscreen"

    Returns:
        {
          "consumer_problems": [...],
          "consumer_expectations": [...],
          "skin_types_mentioned": [...],
          "key_questions": [...],
          "signal_strength": float
        }
    """
    if not config.APIFY_API_TOKEN:
        logger.warning("APIFY_API_KEY 미설정 — 소구점 수집 스킵")
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # 제품명으로 영상 검색
    video_urls = search_videos_by_keyword(product_keyword, max_results=5)

    if not video_urls:
        logger.warning(f"관련 영상 없음: {product_keyword}")
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}

    # 댓글 수집
    comments = scrape_comments(video_urls, max_comments_per_video=100)

    # 소구점 추출
    return extract_pain_points(comments, product_keyword, client)


def collect_pain_points_from_seed_channels() -> list[dict]:
    """
    시드 채널 최근 영상에서 소구점을 수집합니다.
    스케줄러에서 주기적으로 호출합니다.

    Returns:
        [{"channel": ..., "video_urls": [...], "pain_points": {...}}, ...]
    """
    if not config.APIFY_API_TOKEN:
        logger.warning("APIFY_API_KEY 미설정 — 시드 채널 수집 스킵")
        return []

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    results = []

    for channel in SEED_CHANNELS:
        logger.info(f"시드 채널 처리: {channel['name']}")

        video_urls = get_seed_channel_videos(channel["url"], max_results=5)
        if not video_urls:
            continue

        comments = scrape_comments(video_urls, max_comments_per_video=100)
        pain_points = extract_pain_points(comments, channel["name"], client)

        if pain_points.get("signal_strength", 0) >= 0.5:
            results.append({
                "channel":    channel["name"],
                "video_urls": video_urls,
                "pain_points": pain_points,
            })

    logger.info(f"시드 채널 수집 완료: {len(results)}개 채널에서 소구점 추출")
    return results

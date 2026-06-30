"""
collector/youtube_collector.py
영어권 K뷰티 유튜브 영상을 검색하고 댓글을 수집하여 소구점을 추출합니다.

YouTube Data API v3 사용 (Apify 대체):
  - 영상 검색: search.list
  - 댓글 수집: commentThreads.list
  - 무료 쿼터: 하루 10,000 유닛 (검색 100유닛/호출, 댓글 1유닛/호출)
"""
import json
import logging

import anthropic
import requests

from config import config

logger = logging.getLogger(__name__)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

# ─── 시드 채널 ID 목록 ────────────────────────────────────────────
# K뷰티 스킨케어 특화, 영어권 주류 시청자
SEED_CHANNELS = [
    {"name": "Gothamista",    "channel_id": "UC-X4BAoKxJpOXMiMUSK5K8g"},
    {"name": "Glow With Ava", "channel_id": "UCYl_3ML3CxMzHBcTVJbmEgg"},
    {"name": "Joan Kim",      "channel_id": "UCmPgDOHAAi-sHoSZR8hSEjQ"},
    {"name": "Liah Yoo",      "channel_id": "UC3y1VNzSbLLJCpgMhXUWfBQ"},
    {"name": "Soo Beauty",    "channel_id": "UCXuzhVKNOhPjwLDLJBHXFxA"},
]


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
Low (below 0.5) = mostly generic praise, not useful.
Only include problems/expectations that appear in multiple comments or seem highly representative.
"""


# ─── JSON 파싱 헬퍼 ───────────────────────────────────────────────

def _parse_json(raw_text: str):
    """Claude 응답에서 JSON 파싱. ```json 블록 처리 포함."""
    text = raw_text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _is_english(text: str) -> bool:
    """텍스트가 영어인지 간단히 판별합니다 (ASCII 비율 기준)."""
    if not text:
        return False
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars / len(text) > 0.8


# ─── 1단계: 영상 검색 ────────────────────────────────────────────

def search_videos_by_keyword(product_keyword: str, max_results: int = 5) -> list[str]:
    """
    제품명 키워드로 유튜브 영상을 검색하여 video_id 목록을 반환합니다.
    YouTube Data API search.list 사용 (100 유닛/호출)

    Returns:
        ["video_id1", "video_id2", ...]
    """
    query = f"{product_keyword} review korean skincare"
    logger.info(f"YouTube 영상 검색: '{query}'")

    params = {
        "key":        config.GOOGLE_API_KEY,
        "q":          query,
        "part":       "id",
        "type":       "video",
        "maxResults": max_results,
        "relevanceLanguage": "en",
        "videoCaption": "any",
    }

    try:
        resp = requests.get(f"{YOUTUBE_API_BASE}/search", params=params, timeout=15)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
        logger.info(f"영상 {len(video_ids)}개 수집됨: {video_ids}")
        return video_ids
    except Exception as e:
        logger.error(f"영상 검색 실패: {e}")
        return []


def get_channel_videos(channel_id: str, max_results: int = 5) -> list[str]:
    """
    시드 채널에서 최근 영상 video_id를 수집합니다.
    YouTube Data API search.list 사용 (100 유닛/호출)

    Returns:
        ["video_id1", "video_id2", ...]
    """
    logger.info(f"시드 채널 영상 수집: {channel_id}")

    params = {
        "key":        config.GOOGLE_API_KEY,
        "channelId":  channel_id,
        "part":       "id",
        "type":       "video",
        "order":      "date",
        "maxResults": max_results * 3,  # 필터링 감안해 여유 있게
    }

    try:
        resp = requests.get(f"{YOUTUBE_API_BASE}/search", params=params, timeout=15)
        resp.raise_for_status()
        items = resp.json().get("items", [])

        # K뷰티 관련 영상 필터링을 위해 snippet도 요청
        all_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]

        # snippet 정보로 K뷰티 관련 영상 필터링
        kbeauty_keywords = [
            "korean", "k-beauty", "kbeauty", "olive young", "skincare",
            "toner", "serum", "sunscreen", "moisturizer", "haul"
        ]
        # snippet 없이 id만 받았으므로 일단 전체 반환 (max_results 개수 제한)
        filtered = all_ids[:max_results]
        logger.info(f"채널 영상 {len(filtered)}개 수집됨")
        return filtered
    except Exception as e:
        logger.error(f"채널 영상 수집 실패 ({channel_id}): {e}")
        return []


# ─── 2단계: 댓글 수집 ────────────────────────────────────────────

def scrape_comments(video_ids: list[str], max_comments_per_video: int = 100) -> list[dict]:
    """
    video_id 목록에서 댓글을 수집합니다.
    YouTube Data API commentThreads.list 사용 (1 유닛/호출)

    Returns:
        [{"video_id": ..., "comment": ..., "likes": ...}, ...]
    """
    if not video_ids:
        return []

    logger.info(f"댓글 수집 시작: {len(video_ids)}개 영상")
    all_comments = []

    for video_id in video_ids:
        comments = _fetch_comments_for_video(video_id, max_comments_per_video)
        english = [c for c in comments if _is_english(c["comment"])]
        logger.info(f"  └ {video_id} → 전체 {len(comments)}개 / 영어 {len(english)}개")
        all_comments.extend(english)

    logger.info(f"전체 영어 댓글 {len(all_comments)}개 수집됨")
    return all_comments


def _fetch_comments_for_video(video_id: str, max_comments: int) -> list[dict]:
    """
    단일 영상의 댓글을 페이지네이션으로 수집합니다.
    """
    comments = []
    page_token = None

    while len(comments) < max_comments:
        params = {
            "key":        config.GOOGLE_API_KEY,
            "videoId":    video_id,
            "part":       "snippet",
            "order":      "relevance",   # 좋아요 많은 댓글 우선
            "maxResults": min(100, max_comments - len(comments)),
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = requests.get(
                f"{YOUTUBE_API_BASE}/commentThreads",
                params=params,
                timeout=15,
            )
            # 댓글 비활성화된 영상
            if resp.status_code == 403:
                logger.warning(f"  └ {video_id} 댓글 비활성화됨, 스킵")
                break
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("items", []):
                snippet = item["snippet"]["topLevelComment"]["snippet"]
                comments.append({
                    "video_id": video_id,
                    "comment":  snippet.get("textDisplay", ""),
                    "likes":    snippet.get("likeCount", 0),
                })

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        except Exception as e:
            logger.error(f"  └ {video_id} 댓글 수집 오류: {e}")
            break

    return comments


# ─── 3단계: Claude로 소구점 추출 ─────────────────────────────────

def extract_pain_points(
    comments: list[dict],
    product_keyword: str,
    client: anthropic.Anthropic,
) -> dict:
    """
    수집된 댓글에서 consumer_problem / consumer_expectation을 추출합니다.
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
        result = _parse_json(message.content[0].text)
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
    if not config.GOOGLE_API_KEY:
        logger.warning("GOOGLE_API_KEY 미설정 — 소구점 수집 스킵")
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # 제품명으로 영상 검색
    video_ids = search_videos_by_keyword(product_keyword, max_results=5)
    if not video_ids:
        logger.warning(f"관련 영상 없음: {product_keyword}")
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}

    # 댓글 수집
    comments = scrape_comments(video_ids, max_comments_per_video=100)

    # 소구점 추출
    return extract_pain_points(comments, product_keyword, client)


def collect_pain_points_from_seed_channels() -> list[dict]:
    """
    시드 채널 최근 영상에서 소구점을 수집합니다.
    스케줄러에서 주기적으로 호출합니다.

    Returns:
        [{"channel": ..., "video_ids": [...], "pain_points": {...}}, ...]
    """
    if not config.GOOGLE_API_KEY:
        logger.warning("GOOGLE_API_KEY 미설정 — 시드 채널 수집 스킵")
        return []

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    results = []

    for channel in SEED_CHANNELS:
        logger.info(f"시드 채널 처리: {channel['name']}")
        video_ids = get_channel_videos(channel["channel_id"], max_results=5)
        if not video_ids:
            continue

        comments = scrape_comments(video_ids, max_comments_per_video=100)
        pain_points = extract_pain_points(comments, channel["name"], client)

        if pain_points.get("signal_strength", 0) >= 0.5:
            results.append({
                "channel":    channel["name"],
                "video_ids":  video_ids,
                "pain_points": pain_points,
            })

    logger.info(f"시드 채널 수집 완료: {len(results)}개 채널에서 소구점 추출")
    return results

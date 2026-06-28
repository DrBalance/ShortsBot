"""
collector/reddit_collector.py
r/AsianBeauty, r/SkincareAddiction 등에서 K뷰티 소구점을 수집합니다.

Reddit은 유튜브 댓글보다 훨씬 구체적인 피부 고민이 담겨 있어
consumer_problem 심화 소스로 활용합니다.
"""
import json
import logging
import time

import anthropic
import requests

from config import config

logger = logging.getLogger(__name__)

# ─── 수집 대상 서브레딧 ───────────────────────────────────────────
TARGET_SUBREDDITS = [
    "AsianBeauty",
    "SkincareAddiction",
    "kbeauty",
]

REDDIT_SCRAPER_ACTOR = "trudax/reddit-scraper"  # Apify 공식 Reddit Scraper

APIFY_BASE_URL = "https://api.apify.com/v2"


# ─── 소구점 추출 프롬프트 ─────────────────────────────────────────

EXTRACT_SYSTEM_PROMPT = """You are a K-beauty consumer insights analyst.
Analyze Reddit posts and comments to extract consumer pain points and expectations.
Respond only in JSON format. No other text.
"""

EXTRACT_USER_TEMPLATE = """Analyze these Reddit posts/comments from K-beauty subreddits about {product_keyword}.

=== Posts & Comments ===
{posts_text}

Extract the real consumer signals. Focus on:
- Specific skin problems they're trying to solve
- What they've already tried that didn't work
- What they specifically want from a Korean product
- Concerns about ingredients, skin tone compatibility, climate/humidity
- Comparisons or recommendations they make

=== Response Format (JSON) ===
{{
  "consumer_problems": [
    "I have oily skin that gets shiny within 2 hours and most moisturizers make it worse",
    "my sunscreen always pills under makeup no matter what I try"
  ],
  "consumer_expectations": [
    "want a toner that hydrates without making skin feel sticky",
    "looking for a sunscreen with no white cast that works for brown skin"
  ],
  "skin_types_mentioned": ["oily", "combination", "acne-prone", "dark skin tones"],
  "key_phrases": [
    "holy grail",
    "repurchased 5 times",
    "doesn't work on dark skin",
    "pilled under sunscreen"
  ],
  "signal_strength": 0.0
}}

signal_strength: 0.0~1.0. High (0.8+) = very specific, actionable pain points with detail.
Write consumer_problems and consumer_expectations as complete, natural sentences — these will be used directly in video scripts.
"""


# ─── Apify 헬퍼 ──────────────────────────────────────────────────

def _run_apify_actor(actor_id: str, input_data: dict, timeout_secs: int = 120) -> list[dict]:
    """Apify Actor를 실행하고 결과 데이터셋을 반환합니다."""
    headers = {"Content-Type": "application/json"}
    params  = {"token": config.APIFY_API_KEY}

    run_url = f"{APIFY_BASE_URL}/acts/{actor_id}/runs"
    resp = requests.post(run_url, json=input_data, headers=headers, params=params, timeout=30)
    resp.raise_for_status()

    run_id     = resp.json()["data"]["id"]
    dataset_id = resp.json()["data"]["defaultDatasetId"]
    logger.info(f"Apify run started: {actor_id} / run={run_id}")

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

    items_url  = f"{APIFY_BASE_URL}/datasets/{dataset_id}/items"
    items_resp = requests.get(items_url, params={**params, "format": "json"}, timeout=30)
    items_resp.raise_for_status()
    return items_resp.json()


# ─── Reddit 수집 ─────────────────────────────────────────────────

def search_reddit(product_keyword: str, max_posts: int = 20) -> list[dict]:
    """
    제품 키워드로 Reddit 포스트와 댓글을 수집합니다.

    Returns:
        [{"subreddit": ..., "title": ..., "body": ..., "comments": [...]}, ...]
    """
    if not config.APIFY_API_KEY:
        logger.warning("APIFY_API_KEY 미설정 — Reddit 수집 스킵")
        return []

    logger.info(f"Reddit 검색: '{product_keyword}'")

    # 서브레딧별로 검색
    all_posts = []
    for subreddit in TARGET_SUBREDDITS:
        input_data = {
            "startUrls": [{
                "url": f"https://www.reddit.com/r/{subreddit}/search/?q={product_keyword}&restrict_sr=1&sort=relevance"
            }],
            "maxItems": max_posts // len(TARGET_SUBREDDITS),
            "includeComments": True,
            "maxComments": 30,
        }

        try:
            items = _run_apify_actor(REDDIT_SCRAPER_ACTOR, input_data, timeout_secs=120)
            for item in items:
                all_posts.append({
                    "subreddit": subreddit,
                    "title":     item.get("title", ""),
                    "body":      item.get("body") or item.get("selftext", ""),
                    "score":     item.get("score") or item.get("ups", 0),
                    "comments":  item.get("comments", []),
                })
            logger.info(f"r/{subreddit}: {len(items)}개 포스트 수집됨")
        except Exception as e:
            logger.error(f"r/{subreddit} 수집 실패: {e}")
            continue

    return all_posts


# ─── Claude로 소구점 추출 ─────────────────────────────────────────

def extract_pain_points(
    posts: list[dict],
    product_keyword: str,
    client: anthropic.Anthropic,
) -> dict:
    """
    수집된 Reddit 포스트/댓글에서 소구점을 추출합니다.
    """
    if not posts:
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}

    # 점수 높은 순으로 정렬, 상위 20개
    top_posts = sorted(posts, key=lambda x: x["score"], reverse=True)[:20]

    lines = []
    for post in top_posts:
        lines.append(f"[POST] {post['title']}")
        if post["body"]:
            lines.append(post["body"][:300])
        for comment in post.get("comments", [])[:5]:
            comment_body = comment.get("body") or comment.get("text") or ""
            if comment_body:
                lines.append(f"  └ {comment_body[:200]}")
        lines.append("")

    posts_text = "\n".join(lines)

    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=EXTRACT_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": EXTRACT_USER_TEMPLATE.format(
                    product_keyword=product_keyword,
                    posts_text=posts_text,
                ),
            }],
        )
        result = json.loads(message.content[0].text.strip())
        logger.info(
            f"Reddit 소구점 추출 완료: problems={len(result.get('consumer_problems', []))}, "
            f"signal={result.get('signal_strength', 0):.2f}"
        )
        return result
    except Exception as e:
        logger.error(f"소구점 추출 실패: {e}")
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}


# ─── 통합 실행 함수 ───────────────────────────────────────────────

def collect_pain_points_for_product(product_keyword: str) -> dict:
    """
    제품 키워드에 대한 Reddit 소구점을 수집합니다.
    claude_analyzer.py의 분류 단계에서 호출합니다.

    Args:
        product_keyword: 예) "anua toner", "beauty of joseon sunscreen"

    Returns:
        {
          "consumer_problems": [...],
          "consumer_expectations": [...],
          "skin_types_mentioned": [...],
          "key_phrases": [...],
          "signal_strength": float
        }
    """
    if not config.APIFY_API_KEY:
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    posts  = search_reddit(product_keyword, max_posts=20)
    return extract_pain_points(posts, product_keyword, client)


def merge_pain_points(youtube_data: dict, reddit_data: dict) -> dict:
    """
    YouTube 댓글과 Reddit에서 추출한 소구점을 병합합니다.
    중복 제거 없이 합치되, signal_strength는 평균으로 계산합니다.

    claude_analyzer.py의 분류 프롬프트에 주입하는 최종 데이터입니다.
    """
    problems = (
        youtube_data.get("consumer_problems", []) +
        reddit_data.get("consumer_problems", [])
    )
    expectations = (
        youtube_data.get("consumer_expectations", []) +
        reddit_data.get("consumer_expectations", [])
    )
    skin_types = list(set(
        youtube_data.get("skin_types_mentioned", []) +
        reddit_data.get("skin_types_mentioned", [])
    ))
    yt_signal = youtube_data.get("signal_strength", 0.0)
    rd_signal = reddit_data.get("signal_strength", 0.0)

    # 둘 다 있으면 평균, 하나만 있으면 그 값 사용
    if yt_signal and rd_signal:
        signal = (yt_signal + rd_signal) / 2
    else:
        signal = max(yt_signal, rd_signal)

    return {
        "consumer_problems":    problems[:5],    # 상위 5개만 사용
        "consumer_expectations": expectations[:5],
        "skin_types_mentioned": skin_types,
        "signal_strength":      round(signal, 2),
    }

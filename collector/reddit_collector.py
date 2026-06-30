"""
collector/reddit_collector.py
r/AsianBeauty, r/SkincareAddiction 등에서 K뷰티 소구점을 수집합니다.

Reddit 공식 API 사용 (Apify 대체):
  - 인증 불필요: Reddit JSON API (공개 엔드포인트)
  - https://www.reddit.com/r/{subreddit}/search.json
  - 무료, 키 불필요, 분당 60회 요청 제한
"""
import json
import logging
import time

import anthropic
import requests

from config import config

logger = logging.getLogger(__name__)

TARGET_SUBREDDITS = [
    "AsianBeauty",
    "SkincareAddiction",
    "kbeauty",
]

REDDIT_BASE_URL = "https://www.reddit.com"

# Reddit API는 User-Agent 필수
HEADERS = {
    "User-Agent": "ShortsBot/1.0 (K-beauty content research)"
}


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
Write consumer_problems and consumer_expectations as complete, natural sentences.
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


# ─── Reddit 수집 ─────────────────────────────────────────────────

def search_reddit(product_keyword: str, max_posts: int = 20) -> list[dict]:
    """
    제품 키워드로 Reddit 포스트와 댓글을 수집합니다.
    Reddit 공식 JSON API 사용 (인증 불필요)

    Returns:
        [{"subreddit": ..., "title": ..., "body": ..., "score": ..., "comments": [...]}, ...]
    """
    logger.info(f"Reddit 검색: '{product_keyword}'")
    all_posts = []

    for subreddit in TARGET_SUBREDDITS:
        try:
            posts = _search_subreddit(subreddit, product_keyword, max_posts // len(TARGET_SUBREDDITS))
            all_posts.extend(posts)
            logger.info(f"  └ r/{subreddit}: {len(posts)}개 포스트 수집됨")
            time.sleep(1)  # Reddit rate limit 방지
        except Exception as e:
            logger.error(f"  └ r/{subreddit} 수집 실패: {e}")
            continue

    logger.info(f"전체 {len(all_posts)}개 포스트 수집됨")
    return all_posts


def _search_subreddit(subreddit: str, keyword: str, limit: int) -> list[dict]:
    """
    단일 서브레딧에서 키워드로 포스트를 검색합니다.
    """
    url = f"{REDDIT_BASE_URL}/r/{subreddit}/search.json"
    params = {
        "q":           keyword,
        "restrict_sr": 1,
        "sort":        "relevance",
        "limit":       min(limit, 25),  # Reddit 최대 25
        "t":           "year",          # 최근 1년
    }

    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    posts = []
    for child in data.get("data", {}).get("children", []):
        post = child.get("data", {})
        posts.append({
            "subreddit": subreddit,
            "title":     post.get("title", ""),
            "body":      post.get("selftext", "")[:500],
            "score":     post.get("score", 0),
            "url":       post.get("url", ""),
            "permalink": post.get("permalink", ""),
            "comments":  _fetch_comments(post.get("permalink", "")),
        })
        time.sleep(0.5)  # 댓글 요청 간 딜레이

    return posts


def _fetch_comments(permalink: str, max_comments: int = 20) -> list[str]:
    """
    포스트의 댓글을 수집합니다.
    """
    if not permalink:
        return []

    try:
        url = f"{REDDIT_BASE_URL}{permalink}.json"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        comments = []
        # data[1]이 댓글 목록
        if len(data) > 1:
            for child in data[1].get("data", {}).get("children", [])[:max_comments]:
                body = child.get("data", {}).get("body", "")
                if body and body != "[deleted]" and body != "[removed]":
                    comments.append(body[:300])

        return comments
    except Exception:
        return []


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
        lines.append(f"[POST r/{post['subreddit']}] {post['title']}")
        if post["body"]:
            lines.append(post["body"])
        for comment in post.get("comments", [])[:5]:
            if comment:
                lines.append(f"  └ {comment}")
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
        result = _parse_json(message.content[0].text)
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
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    posts  = search_reddit(product_keyword, max_posts=20)
    return extract_pain_points(posts, product_keyword, client)


def merge_pain_points(youtube_data: dict, reddit_data: dict) -> dict:
    """
    YouTube 댓글과 Reddit에서 추출한 소구점을 병합합니다.
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

    if yt_signal and rd_signal:
        signal = (yt_signal + rd_signal) / 2
    else:
        signal = max(yt_signal, rd_signal)

    return {
        "consumer_problems":     problems[:5],
        "consumer_expectations": expectations[:5],
        "skin_types_mentioned":  skin_types,
        "signal_strength":       round(signal, 2),
    }

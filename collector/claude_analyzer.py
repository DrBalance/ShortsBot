"""
collector/claude_analyzer.py
수집된 인스타 게시물을 Claude로 분석하여 쇼츠 콘텐츠 후보를 생성합니다.
"""
import json
import logging
from typing import Optional

import anthropic
from config import config
from db import supabase_client as db

logger = logging.getLogger(__name__)

# ─── 프롬프트 ─────────────────────────────────────────────────

ANALYSIS_SYSTEM_PROMPT = """당신은 K뷰티 트렌드 분석 전문가입니다.
인스타그램 게시물 캡션을 분석하여 유튜브 쇼츠에 활용 가능한 콘텐츠 소재를 추출합니다.

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요.
"""

ANALYSIS_USER_TEMPLATE = """다음 인스타그램 게시물들을 분석하여 유튜브 쇼츠 콘텐츠 후보를 생성해주세요.

=== 게시물 목록 ===
{posts_text}

=== 요구사항 ===
각 게시물에서 다음을 추출하세요:
1. trend_topic: 핵심 트렌드 주제 (예: "SPF50+ 선크림", "비타민C 세럼")
2. products: 언급된 제품명 목록 (올리브영에서 판매하는 제품 위주)
3. keywords: SEO용 핵심 키워드 5개
4. relevance_score: K뷰티 쇼츠 콘텐츠 적합도 (0.0 ~ 1.0)
   - 0.9+: 제품 정보, 가격, 순위, 루틴 등 명확한 정보성 콘텐츠
   - 0.7+: 트렌드 소개, 신상 소개
   - 0.5 미만: 단순 일상, 광고 느낌 강함
5. shorts_title: 유튜브 쇼츠 제목 (한국어, 30자 이내, 궁금증/숫자/트렌드 활용)
6. shorts_script: 60초 분량 나레이션 스크립트 (한국어, 자연스러운 구어체, 약 300자)
   - 첫 3초: 강력한 후킹
   - 중반: 핵심 정보 2~3가지
   - 마지막: 구매/구독 유도
7. hook_line: 첫 3초 후킹 문구 (15자 이내)

응답 형식 (JSON 배열):
[
  {{
    "post_index": 0,
    "trend_topic": "...",
    "products": ["제품1", "제품2"],
    "keywords": ["키워드1", "키워드2", "키워드3", "키워드4", "키워드5"],
    "relevance_score": 0.85,
    "shorts_title": "...",
    "shorts_script": "...",
    "hook_line": "..."
  }}
]

relevance_score가 0.6 미만인 게시물은 배열에서 제외하세요."""


def _build_posts_text(posts: list[dict]) -> str:
    """게시물 목록을 프롬프트용 텍스트로 변환."""
    lines = []
    for i, post in enumerate(posts):
        caption = (post.get("caption") or "캡션 없음")[:500]
        lines.append(
            f"[{i}] 해시태그: #{post['hashtag']} | "
            f"좋아요: {post['likes_count']} | "
            f"캡션: {caption}"
        )
    return "\n\n".join(lines)


def analyze_posts(posts: list[dict]) -> list[dict]:
    """
    게시물 목록을 Claude로 분석하여 콘텐츠 후보 목록을 반환합니다.
    
    Args:
        posts: DB에서 가져온 raw_posts 목록
    Returns:
        분석 결과 목록 (relevance_score >= 0.6만 포함)
    """
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    posts_text = _build_posts_text(posts)

    logger.info(f"Claude 분석 시작: {len(posts)}개 게시물")

    message = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=4096,
        system=ANALYSIS_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": ANALYSIS_USER_TEMPLATE.format(posts_text=posts_text),
            }
        ],
    )

    raw_text = message.content[0].text.strip()

    # JSON 파싱 (```json 블록 처리)
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]

    results = json.loads(raw_text)
    logger.info(f"분석 결과: {len(results)}개 후보 추출")
    return results


def run_analysis(batch_size: int = 10) -> int:
    """
    미분석 게시물을 가져와 Claude 분석 후 후보 DB에 저장합니다.
    스케줄러에서 호출하는 진입점.
    
    Returns:
        저장된 콘텐츠 후보 수
    """
    posts = db.get_unprocessed_posts(limit=batch_size)
    if not posts:
        logger.info("분석할 게시물 없음")
        return 0

    try:
        analysis_results = analyze_posts(posts)
    except Exception as e:
        logger.error(f"Claude 분석 실패: {e}")
        return 0

    # 결과를 DB에 저장
    saved = 0
    for result in analysis_results:
        idx = result.get("post_index", 0)
        if idx >= len(posts):
            continue

        post = posts[idx]
        candidate = {
            "raw_post_id": post["id"],
            "trend_topic": result["trend_topic"],
            "products": result["products"],
            "keywords": result["keywords"],
            "relevance_score": result["relevance_score"],
            "shorts_title": result["shorts_title"],
            "shorts_script": result["shorts_script"],
            "hook_line": result["hook_line"],
            "status": "pending",
        }

        if db.insert_candidate(candidate):
            db.mark_post_processed(post["id"])
            saved += 1

    # 분석했지만 후보로 선정 안 된 게시물도 processed 처리
    analyzed_indices = {r.get("post_index", 0) for r in analysis_results}
    for i, post in enumerate(posts):
        if i not in analyzed_indices:
            db.mark_post_processed(post["id"])

    logger.info(f"분석 완료: {saved}개 후보 저장")
    return saved

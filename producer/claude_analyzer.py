"""
collector/claude_analyzer.py
수집된 인스타 게시물을 Claude로 분석하여 쇼츠 콘텐츠 후보를 생성합니다.

콘텐츠 유형 3가지:
  - 새소식: 정보 격차형 — 한국에서 뜨는데 동남아에 아직 없는 것
  - 문제추천: Problem Oriented Solution — 소비자 문제에서 출발하는 제품 추천
  - 관심상품: 타인들의 선택 분석 — TOP3/4, 베스트셀러, 스테디셀러
"""
import json
import logging

import anthropic
from config import config
from db import supabase_client as db

logger = logging.getLogger(__name__)


# ─── 1단계: 분류 프롬프트 ────────────────────────────────────────

CLASSIFY_SYSTEM_PROMPT = """당신은 K뷰티 콘텐츠 전략가입니다.
인스타그램 게시물을 분석하여 유튜브 쇼츠 콘텐츠 유형을 분류합니다.

반드시 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요.
"""

CLASSIFY_USER_TEMPLATE = """다음 인스타그램 게시물들을 분석하여 콘텐츠 유형을 분류해주세요.

=== 게시물 목록 ===
{posts_text}

=== 콘텐츠 유형 정의 ===

1. 새소식 (정보격차형)
   - 한국에서 최근 화제가 된 신제품/트렌드
   - 동남아 소비자가 아직 모를 가능성이 높은 정보
   - 용도/특징을 몰라서 관심이 없었던 제품을 알려주는 것
   - 예: 품절대란 선크림, 올리브영 신상, 한국 인스타 난리난 제품

2. 문제추천 (Problem Oriented Solution)
   - 특정 피부 고민/문제 상황이 명확히 존재
   - 그 문제를 해결하는 제품을 추천하는 구조
   - 예: 지성피부 선크림 번들거림, 민감성 피부 보습, 여름 모공 관리

3. 관심상품 (타인들의 선택 분석)
   - 판매 순위, 베스트셀러, 스테디셀러, 세일 추천
   - 다른 사람들이 많이 선택한 제품을 보여주는 구조
   - 제품이 3~4개 묶음으로 소개되는 경우
   - 예: 올리브영 이달 TOP3, 동남아에서 가장 많이 팔린 K뷰티

=== 응답 형식 (JSON 배열) ===
[
  {{
    "post_index": 0,
    "content_type": "새소식 | 문제추천 | 관심상품",
    "relevance_score": 0.0 ~ 1.0,
    "trend_topic": "핵심 트렌드 주제",
    "products": ["제품1", "제품2"],
    "consumer_problem": "소비자가 겪는 문제 (문제추천 유형만, 나머지는 null)",
    "consumer_expectation": "소비자 기대감/니즈 (새소식 유형, 인스타 댓글/반응에서 추출)",
    "keywords": ["키워드1", "키워드2", "키워드3", "키워드4", "키워드5"]
  }}
]

relevance_score 기준:
- 0.9+: 제품 정보, 가격, 순위, 루틴 등 명확한 정보성
- 0.7+: 트렌드 소개, 신상 소개
- 0.6 미만: 단순 일상, 광고 느낌 강함 → 배열에서 제외
"""


# ─── 2단계: 스크립트 생성 프롬프트 ──────────────────────────────

SCRIPT_SYSTEM_PROMPT = """당신은 K뷰티 유튜브 쇼츠 스크립트 작가입니다.
동남아 시장을 타겟으로 한국 K뷰티 정보를 32초 분량의 나레이션으로 작성합니다.

반드시 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요.
"""

# 유형별 시나리오 구조 정의
SCENARIO_TEMPLATES = {
    "새소식": """
=== 새소식 시나리오 구조 (총 32초) ===
- Hook (3초): 자극적 키워드로 시작. "품절", "대란", "난리" 등 사용. 제품명/카테고리 포함.
- 공감유발 (5초): 소비자 기대감/니즈를 직접 언급. "이런 상황 있죠?" 형태.
  → consumer_expectation 데이터 활용
- 용도설명 (13초): 이 제품이 그 상황을 어떻게 해결하는지. 핵심 특징 1~2가지만.
- 증거 (9초): 한국 반응 데이터. 게시물 수, 좋아요, 실제 반응 언급.
- 구독유도 (2초): "한국 K뷰티 새소식 가장 빠르게 받고 싶다면 구독"

끝맺음: 구독 유도 (구매 언급 없음, 링크는 설명란에만)
""",
    "문제추천": """
=== 문제추천 시나리오 구조 (총 32초) ===
- Hook (3초): 문제 상황을 직접 언급. "~한 사람?" 형태로 공감 유도.
  → consumer_problem 데이터 활용
- 문제심화 (4초): 왜 기존 방법으로 해결이 안 됐는지.
- 솔루션 (16초): 이 제품이 어떻게 다른지. 성분/특징/효과 구체적으로.
  → 납득이 되어야 구매로 이어지므로 가장 긴 파트
- 증거 (7초): 비슷한 피부 타입 한국 반응.
- 구매유도 (2초): "링크는 설명란에"

끝맺음: 구매 유도
""",
    "관심상품": """
=== 관심상품 시나리오 구조 (총 32초) ===
- Hook (3초): 순위/숫자로 시작. "이번 달 올리브영 가장 많이 팔린 ~"
- 제품 소개 (27초): 제품 수에 따라 균등 배분
  → 3개: 제품당 9초 (3위→2위→1위 순서)
  → 4개: 제품당 6~7초
  각 제품마다: 제품명 + 핵심 특징 한 가지 + 왜 선택받는지
- 구매유도 (2초): "링크는 설명란에"

끝맺음: 구매 유도
제품은 products 목록에서 3~4개 선택. 없으면 trend_topic 기반으로 대표 제품 설정.
""",
}

SCRIPT_USER_TEMPLATE = """다음 K뷰티 콘텐츠 후보의 스크립트를 작성해주세요.

=== 콘텐츠 정보 ===
유형: {content_type}
트렌드 주제: {trend_topic}
제품 목록: {products}
소비자 문제: {consumer_problem}
소비자 기대감: {consumer_expectation}
키워드: {keywords}

{scenario_template}

=== 작성 규칙 ===
- 언어: 한국어, 자연스러운 구어체
- 총 32초 분량 (약 160~180자)
- 각 파트를 [Hook], [공감유발] 등 태그로 구분하여 작성
- 과장·자극적 표현은 Hook에만 사용, 나머지는 팩트 중심
- 동남아 시청자 기준: 한국 문화 배경 설명 불필요, 제품 정보 중심

=== 응답 형식 (JSON) ===
{{
  "shorts_title": "유튜브 쇼츠 제목 (30자 이내, 숫자/트렌드/궁금증 활용)",
  "hook_line": "첫 3초 후킹 문구 (15자 이내)",
  "shorts_script": "전체 나레이션 스크립트 ([Hook] ... [공감유발] ... 형태로 파트 구분)",
  "script_duration_sec": 32
}}
"""


# ─── 핵심 함수 ───────────────────────────────────────────────────

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


def _parse_json(raw_text: str) -> any:
    """Claude 응답에서 JSON 파싱. ```json 블록 처리 포함."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def classify_posts(posts: list[dict], client: anthropic.Anthropic) -> list[dict]:
    """
    1단계: 게시물을 세 가지 콘텐츠 유형으로 분류.
    
    Returns:
        분류 결과 목록 (relevance_score >= 0.6만 포함)
    """
    posts_text = _build_posts_text(posts)
    logger.info(f"분류 시작: {len(posts)}개 게시물")

    message = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=4096,
        system=CLASSIFY_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": CLASSIFY_USER_TEMPLATE.format(posts_text=posts_text),
        }],
    )

    results = _parse_json(message.content[0].text)
    logger.info(f"분류 완료: {len(results)}개 후보")
    return results


def generate_script(classified: dict, client: anthropic.Anthropic) -> dict:
    """
    2단계: 분류된 후보 하나에 대해 유형별 시나리오로 스크립트 생성.
    
    Args:
        classified: classify_posts 결과 항목 하나
    Returns:
        shorts_title, hook_line, shorts_script 포함한 dict
    """
    content_type = classified.get("content_type", "새소식")
    scenario_template = SCENARIO_TEMPLATES.get(content_type, SCENARIO_TEMPLATES["새소식"])

    message = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1024,
        system=SCRIPT_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": SCRIPT_USER_TEMPLATE.format(
                content_type=content_type,
                trend_topic=classified.get("trend_topic", ""),
                products=", ".join(classified.get("products", [])),
                consumer_problem=classified.get("consumer_problem") or "해당없음",
                consumer_expectation=classified.get("consumer_expectation") or "해당없음",
                keywords=", ".join(classified.get("keywords", [])),
                scenario_template=scenario_template,
            ),
        }],
    )

    return _parse_json(message.content[0].text)


def analyze_posts(posts: list[dict]) -> list[dict]:
    """
    게시물 목록을 분류 → 스크립트 생성 두 단계로 처리.
    
    Args:
        posts: DB에서 가져온 raw_posts 목록
    Returns:
        최종 콘텐츠 후보 목록
    """
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # 1단계: 분류
    classified_list = classify_posts(posts, client)

    # 2단계: 유형별 스크립트 생성
    results = []
    for classified in classified_list:
        try:
            script = generate_script(classified, client)
            results.append({**classified, **script})
            logger.info(
                f"스크립트 생성 완료: [{classified['content_type']}] "
                f"{script.get('shorts_title', '')}"
            )
        except Exception as e:
            logger.error(f"스크립트 생성 실패 (post_index={classified.get('post_index')}): {e}")
            continue

    logger.info(f"전체 완료: {len(results)}개 후보 생성")
    return results


def run_analysis(batch_size: int = 10) -> int:
    """
    미분석 게시물을 가져와 분류 + 스크립트 생성 후 후보 DB에 저장.
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
        logger.error(f"분석 실패: {e}")
        return 0

    saved = 0
    for result in analysis_results:
        idx = result.get("post_index", 0)
        if idx >= len(posts):
            continue

        post = posts[idx]
        candidate = {
            "raw_post_id": post["id"],
            "content_type": result["content_type"],        # 신규
            "trend_topic": result["trend_topic"],
            "products": result["products"],
            "keywords": result["keywords"],
            "relevance_score": result["relevance_score"],
            "consumer_problem": result.get("consumer_problem"),   # 신규
            "consumer_expectation": result.get("consumer_expectation"),  # 신규
            "shorts_title": result["shorts_title"],
            "shorts_script": result["shorts_script"],
            "hook_line": result["hook_line"],
            "status": "pending",
        }

        if db.insert_candidate(candidate):
            db.mark_post_processed(post["id"])
            saved += 1

    # 후보로 선정 안 된 게시물도 processed 처리
    analyzed_indices = {r.get("post_index", 0) for r in analysis_results}
    for i, post in enumerate(posts):
        if i not in analyzed_indices:
            db.mark_post_processed(post["id"])

    logger.info(f"분석 완료: {saved}개 후보 저장")
    return saved

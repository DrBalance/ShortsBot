"""
analyzer/claude_analyzer.py
수집된 인스타 게시물을 Claude로 분석하여 쇼츠 콘텐츠 후보를 생성합니다.

파이프라인:
  인스타(한/일) → 트렌드 분류 → 제품명 추출
  → YouTube 댓글 → 소구점 수집 (Reddit은 추후 추가 예정)
  → 영어 스크립트 생성

콘텐츠 유형 3가지:
  - 새소식: 정보 격차형 — 한국에서 뜨는 제품을 영어권이 아직 모름
  - 문제추천: Problem Oriented Solution — 소비자 문제에서 출발하는 제품 추천
  - 관심상품: 타인들의 선택 분석 — TOP3/4, 베스트셀러, 스테디셀러
"""
import json
import logging

import anthropic
from config import config
from db import supabase_client as db
from collector.youtube_collector import collect_pain_points_for_product as yt_pain_points

logger = logging.getLogger(__name__)


# ─── 1단계: 분류 프롬프트 ────────────────────────────────────────

CLASSIFY_SYSTEM_PROMPT = """You are a K-beauty content strategist.
Analyze Korean/Japanese Instagram posts and classify them into YouTube Shorts content types
targeting English-speaking audiences (US, UK, Australia, Canada).

Respond only in JSON format. No other text.
"""

CLASSIFY_USER_TEMPLATE = """Analyze these Instagram posts and classify them for English-speaking K-beauty audiences.

=== Posts ===
{posts_text}

=== Content Type Definitions ===

1. new_find (Information Gap)
   - Products recently trending in Korea that English-speaking audiences haven't discovered yet
   - New launches, viral products, sold-out items
   - Ex: sold-out sunscreen, new Olive Young launch, product going viral on Korean Instagram

2. problem_solution (Problem Oriented Solution)
   - A clear skin problem/concern exists
   - Recommends a specific K-beauty product as the solution
   - Ex: sunscreen that pills under makeup, oily skin moisturizer, sensitive skin routine

3. top_picks (Social Proof / Rankings)
   - Sales rankings, bestsellers, steady sellers, sale recommendations
   - Multiple products (3~4) shown together
   - Ex: Olive Young top 3 this month, most repurchased K-beauty toners

=== Response Format (JSON array) ===
[
  {{
    "post_index": 0,
    "content_type": "new_find | problem_solution | top_picks",
    "relevance_score": 0.0,
    "trend_topic": "core trend topic in English",
    "products": ["Product Name 1", "Product Name 2"],
    "consumer_problem": "skin problem this solves (problem_solution only, else null)",
    "consumer_expectation": "what result/benefit they want (new_find only, else null)",
    "keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]
  }}
]

relevance_score criteria:
- 0.9+: clear product info, rankings, ingredients, routine — strong content potential
- 0.7+: trend intro, new launch — moderate potential
- below 0.6: generic lifestyle, strong ad feel → exclude from array
"""


# ─── 2단계: 스크립트 생성 프롬프트 ──────────────────────────────

SCRIPT_SYSTEM_PROMPT = """You are a K-beauty YouTube Shorts scriptwriter.
Write English narration scripts targeting US/UK/Australian/Canadian audiences,
timed to fit an exact set of scene slots (in seconds) derived from beat-synced music.
Use natural, conversational English — like talking to a friend, not reading an ad.

Respond only in JSON format. No other text.
"""

# content_type별 "장면 역할" 정의.
# 실제 초 배분은 beat_sync.py가 계산한 scenes(장면 개수 + 각 duration)를 그대로 따른다.
# 여기서는 몇 번째 장면이 어떤 서사 역할을 맡는지만 정의한다.
SCENARIO_ROLE_TEMPLATES = {
    "new_find": """
=== new_find Scene Roles (Information Gap) ===
Map the narration to the given scene slots in this order:
1. Hook — Lead with urgency. Use words like "sold out", "going viral", "everyone in Korea is".
   Include product/category name.
2. Relate — Speak directly to the viewer's desire or situation. → Use consumer_expectation data.
3. What it does — Explain how this product delivers that result. 1~2 key features only.
   (If more than 4 scenes exist, this role may span multiple consecutive scenes.)
4. Proof — Korean engagement data: post count, likes, sell-out speed.
5. Subscribe CTA (last scene) — "Follow for K-beauty finds before they blow up globally"

No purchase mention — link goes in description only.
""",
    "problem_solution": """
=== problem_solution Scene Roles (Problem Oriented Solution) ===
Map the narration to the given scene slots in this order:
1. Hook — Call out the skin problem directly. "If your [problem], you need to hear this."
   → Use consumer_problem data.
2. Agitate — Why the usual products fail at solving this.
3. Solution — How this K-beauty product is different. Be specific — ingredients, texture, result.
   (This is the core conviction section — if more than 4 scenes exist, this role may span
   multiple consecutive scenes, since conviction drives purchase.)
4. Proof — Korean community reaction from people with the same skin type.
5. Buy CTA (last scene) — "Link in description"
""",
    "top_picks": """
=== top_picks Scene Roles (Social Proof / Rankings) ===
Map the narration to the given scene slots in this order:
1. Hook — Lead with number/ranking. "The [#] most repurchased K-beauty [category] right now"
2..N-1. Product Rundown — Distribute products roughly evenly across the remaining scenes
   before the last one (reveal order: lowest rank → highest rank, e.g. 3rd → 2nd → 1st).
   Each product gets: name + one key differentiator + why people keep buying it.
N. Buy CTA (last scene) — "Links in description"

Choose 3~4 products from the products list. If insufficient, use trend_topic to set
representative products.
""",
}

SCRIPT_USER_TEMPLATE = """Write an English YouTube Shorts script for this K-beauty content.

=== Content Info ===
Type: {content_type}
Trend Topic: {trend_topic}
Products: {products}
Consumer Problem: {consumer_problem}
Consumer Expectation: {consumer_expectation}
Keywords: {keywords}

=== Real Audience Insights (from YouTube comments) ===
Pain Points:
{pain_points}

Expectations:
{expectations}

Skin types in audience: {skin_types}

=== Scene Slots (beat-synced, in seconds — from beat_sync.py) ===
Total duration: {total_duration:.1f}s across {scene_count} scenes.
Each scene shows its target word count. This is a HARD constraint — staying within
±2 words of the target is mandatory. Count words carefully before finalizing each scene.
{scene_slots_text}

{scenario_role_template}

=== Writing Rules ===
- Language: Natural conversational English (US/UK tone)
- Total duration: {total_duration:.1f} seconds — this comes from the actual music edit,
  not a fixed script length. Do not pad or shorten to hit a round number.
- Each item in "scenes" in your response must correspond 1:1 to a Scene Slot above,
  in the same order, and must fit within that scene's duration.
- Hook only can be dramatic — rest should be factual and specific
- Avoid generic beauty language ("amazing", "holy grail") — be concrete
- If skin types are diverse, briefly acknowledge ("works across skin types" or
  "especially for oily skin")

=== Response Format (JSON) ===
{{
  "shorts_title": "YouTube Shorts title (under 60 chars, use numbers/curiosity/trend)",
  "hook_line": "First scene's hook line (under 10 words)",
  "scenes": [
    {{
      "scene_index": 0,
      "start": 0.0,
      "end": 0.0,
      "role": "hook | relate | solution | proof | product_rundown | cta | ...",
      "text": "narration text for this scene only"
    }}
  ],
  "shorts_script": "Full narration, all scenes joined with [ROLE] section tags",
  "script_duration_sec": {total_duration:.1f}
}}
"""


def _format_scene_slots(scenes: list[dict]) -> str:
    """
    beat_sync.py의 scenes 리스트를 프롬프트에 넣을 텍스트로 변환.
    씬별 목표 단어 수를 계산해서 명시적으로 포함한다.

    Args:
        scenes: [{"index": int, "start": float, "end": float, "duration": float}, ...]
                BeatSyncResult.scenes를 to_dict() 한 리스트, 또는 동일 스키마의 dict 리스트.
    """
    # Sarah 보이스 실측 기반 (2026-07-05 캘리브레이션):
    # 실측 평균: 14~18단어 텍스트에서 2.8~3.0 wps로 발화됨
    # hook 씬도 비슷한 속도로 읽히므로 구분하지 않음
    WORDS_PER_SEC = 2.8
    lines = []
    for i, s in enumerate(scenes):
        target_words = round(s['duration'] * WORDS_PER_SEC)
        lines.append(
            f"  - Scene {s['index']}: {s['start']:.2f}s ~ {s['end']:.2f}s "
            f"(duration {s['duration']:.2f}s → write exactly ~{target_words} words)"
        )
    return "\n".join(lines)


# ─── 핵심 함수 ───────────────────────────────────────────────────

def _build_posts_text(posts: list[dict]) -> str:
    """게시물 목록을 프롬프트용 텍스트로 변환."""
    lines = []
    for i, post in enumerate(posts):
        caption = (post.get("caption") or "no caption")[:500]
        lines.append(
            f"[{i}] hashtag: #{post['hashtag']} | "
            f"likes: {post['likes_count']} | "
            f"caption: {caption}"
        )
    return "\n\n".join(lines)


def _parse_json(raw_text: str) -> any:
    """Claude 응답에서 JSON 파싱."""
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


def _collect_pain_points(products: list[str], cache: dict | None = None) -> dict:
    """
    제품 목록에서 소구점을 수집합니다.
    YouTube 댓글 기반 (Reddit은 추후 추가 예정).

    Args:
        cache: {product_keyword: pain_points} 형태의 메모리 캐시.
               같은 배치 안에서 동일 제품이 여러 후보에 등장할 때
               YouTube API 중복 호출을 막기 위함.
    """
    if not products:
        return {"consumer_problems": [], "consumer_expectations": [], "signal_strength": 0.0}

    # 첫 번째 제품으로 검색 (가장 대표 제품)
    keyword = products[0]

    if cache is not None and keyword in cache:
        logger.info(f"소구점 캐시 적중: '{keyword}' (YouTube 재수집 스킵)")
        return cache[keyword]

    logger.info(f"소구점 수집 시작: '{keyword}'")
    pain_points = yt_pain_points(keyword)

    logger.info(
        f"소구점 수집 완료: problems={len(pain_points.get('consumer_problems', []))}, "
        f"signal={pain_points.get('signal_strength', 0.0)}"
    )

    if cache is not None:
        cache[keyword] = pain_points

    return pain_points


def generate_script(classified: dict, scenes: list[dict], client: anthropic.Anthropic) -> dict:
    """
    2단계: 분류된 후보 하나에 대해, beat_sync.py가 계산한 장면 슬롯에 맞춰
    영어 스크립트를 생성.

    Args:
        classified: classify_posts 결과 항목 하나 (pain_points 포함)
        scenes: beat_sync.compute_scene_slots(...).scenes를 dict 리스트로 변환한 것
                (각 항목: {"index", "start", "end", "duration"})
    Returns:
        shorts_title, hook_line, shorts_script, scenes(장면별 텍스트) 포함한 dict
    """
    if not scenes:
        raise ValueError(
            "scenes가 비어있습니다. generate_script는 beat_sync.py의 장면 슬롯 없이는 "
            "호출할 수 없습니다 (32초 고정 방식은 폐기됨, TODO Phase 1-3)."
        )

    content_type       = classified.get("content_type", "new_find")
    scenario_role_text = SCENARIO_ROLE_TEMPLATES.get(content_type, SCENARIO_ROLE_TEMPLATES["new_find"])
    pain_points         = classified.get("pain_points", {})

    total_duration = scenes[-1]["end"] - scenes[0]["start"]

    message = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1536,
        system=SCRIPT_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": SCRIPT_USER_TEMPLATE.format(
                content_type=content_type,
                trend_topic=classified.get("trend_topic", ""),
                products=", ".join(classified.get("products", [])),
                consumer_problem=(
                    classified.get("consumer_problem") or
                    "; ".join(pain_points.get("consumer_problems", [])[:2]) or
                    "N/A"
                ),
                consumer_expectation=(
                    classified.get("consumer_expectation") or
                    "; ".join(pain_points.get("consumer_expectations", [])[:2]) or
                    "N/A"
                ),
                keywords=", ".join(classified.get("keywords", [])),
                pain_points="\n".join(
                    f"- {p}" for p in pain_points.get("consumer_problems", [])
                ) or "N/A",
                expectations="\n".join(
                    f"- {e}" for e in pain_points.get("consumer_expectations", [])
                ) or "N/A",
                skin_types=", ".join(pain_points.get("skin_types_mentioned", [])) or "general",
                total_duration=total_duration,
                scene_count=len(scenes),
                scene_slots_text=_format_scene_slots(scenes),
                scenario_role_template=scenario_role_text,
            ),
        }],
    )

    return _parse_json(message.content[0].text)


def analyze_posts(posts: list[dict], scenes: list[dict]) -> list[dict]:
    """
    게시물 목록을 분류 → 소구점 수집 → 스크립트 생성 세 단계로 처리.

    Args:
        scenes: beat_sync.compute_scene_slots(...).scenes를 dict 리스트로 변환한 것
                (각 항목: {"index", "start", "end", "duration"}).
                필수 인자 — 실제 음원 기반 장면 슬롯 없이는 스크립트를 생성하지 않는다.
                호출 전에 beat_sync.compute_scene_slots(audio_path)로 먼저 계산할 것.
    """
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    if not scenes:
        raise ValueError(
            "scenes가 비어있습니다. beat_sync.compute_scene_slots(audio_path)로 먼저 "
            "장면 슬롯을 계산한 뒤 넘겨야 합니다. (Phase 1-3 확정: 폴백 없음)"
        )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # 1단계: 분류
    classified_list = classify_posts(posts, client)

    # 2단계: 소구점 수집 + 스크립트 생성
    # pain_points_cache: 같은 배치 안에서 동일 제품이 여러 후보에 등장할 때
    # YouTube 댓글 수집을 한 번만 수행하기 위한 캐시
    pain_points_cache: dict = {}
    results = []
    for classified in classified_list:
        try:
            # 소구점 수집 (캐시 적용)
            pain_points = _collect_pain_points(classified.get("products", []), cache=pain_points_cache)
            classified["pain_points"] = pain_points

            # 영어 스크립트 생성 (beat_sync 장면 슬롯 기준)
            script = generate_script(classified, scenes, client)
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


def run_analysis(batch_size: int, scenes: list[dict]) -> int:
    """
    미분석 게시물을 가져와 분류 + 소구점 수집 + 스크립트 생성 후 후보 DB에 저장.
    스케줄러에서 호출하는 진입점.

    Args:
        scenes: beat_sync.compute_scene_slots(audio_path).scenes를 dict 리스트로 변환한 것.
                필수 인자. 실제 배치 실행 전 음원을 골라 beat_sync로 계산해둘 것.
    """
    posts = db.get_unprocessed_posts(limit=batch_size)
    if not posts:
        logger.info("분석할 게시물 없음")
        return 0

    try:
        analysis_results = analyze_posts(posts, scenes=scenes)
    except Exception as e:
        logger.error(f"분석 실패: {e}")
        return 0

    saved = 0
    for result in analysis_results:
        idx = result.get("post_index", 0)
        if idx >= len(posts):
            continue

        post      = posts[idx]
        pain_data = result.get("pain_points", {})

        candidate = {
            "raw_post_id":           post["id"],
            "content_type":          result["content_type"],
            "trend_topic":           result["trend_topic"],
            "products":              result["products"],
            "keywords":              result["keywords"],
            "relevance_score":       result["relevance_score"],
            "consumer_problem":      (
                result.get("consumer_problem") or
                "; ".join(pain_data.get("consumer_problems", [])[:2])
            ),
            "consumer_expectation":  (
                result.get("consumer_expectation") or
                "; ".join(pain_data.get("consumer_expectations", [])[:2])
            ),
            "shorts_title":          result["shorts_title"],
            "shorts_script":         result["shorts_script"],
            "hook_line":             result["hook_line"],
            "scenes":                result.get("scenes", []),          # 장면별 텍스트+타이밍 (Phase 2에서 사용)
            "script_duration_sec":   result.get("script_duration_sec"),  # beat_sync 기반 실제 길이 (35초+)
            "status":                "pending",
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

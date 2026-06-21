"""
script_generator.py

1단계에서 수집된 콘텐츠 후보(kbeauty_content_candidates)를 받아서:
- 한국어 60초 나레이션 스크립트 생성
- 장면별 이미지 프롬프트 생성 (빠나나AI 스타일)
- 다국어 자막용 번역 생성
"""

import json
import anthropic
from typing import Optional

client = anthropic.Anthropic()

# 빠나나AI에서 추출한 한국인 모델 기본 프롬프트 구조
# 왼손AI가 최적화한 구조 그대로 적용
KOREAN_MODEL_BASE_PROMPT = """A photorealistic portrait of a native South Korean (한국인) female model, \
mid 20s (24-26). natural warm skin tone, long straight silky hair. \
Facial features: large bright eyes with a slim V-line jawline, delicate refined features. \
slim slender body. The model has an innocent pure and fresh aura. \
Born and raised in South Korea. Typical Korean facial bone structure, \
natural Korean skin texture, Korean makeup style (dewy glass skin, gradient lips, soft brow). \
NOT Korean-American or diaspora look. \
IMPORTANT: hair color and style must follow the prompt description exactly. \
Professional fashion photography, soft natural lighting, \
plain solid-color background, no visible studio equipment or props, \
ultra-realistic, sharp focus, 8K quality."""


def generate_shorts_script(candidate: dict) -> dict:
    """
    콘텐츠 후보 데이터를 받아 쇼츠 제작에 필요한 전체 데이터를 생성.

    Args:
        candidate: kbeauty_content_candidates 테이블의 row
            - trend_topic: 트렌드 주제
            - products: 언급된 제품 목록 (JSONB)
            - keywords: 핵심 키워드 (JSONB)
            - shorts_title: 기존 제목 (있을 경우)

    Returns:
        {
            "title": "영상 제목",
            "hook_line": "첫 3초 후킹 문구",
            "script": "전체 나레이션 스크립트 (60초, 약 200자)",
            "scenes": [
                {
                    "order": 1,
                    "duration_sec": 5,
                    "narration": "이 장면 나레이션",
                    "character_prompt": "캐릭터 이미지 생성 프롬프트 (영어)",
                    "location_prompt": "배경 이미지 생성 프롬프트 (영어)",
                    "action_prompt": "Kling용 모션 프롬프트 (영어)",
                    "product_placement": true/false
                },
                ...
            ],
            "subtitles": {
                "ko": "한국어 자막",
                "en": "English subtitle",
                "th": "ภาษาไทย",
                "vi": "Tiếng Việt",
                "id": "Bahasa Indonesia"
            }
        }
    """
    trend_topic = candidate.get("trend_topic", "")
    products = candidate.get("products", [])
    keywords = candidate.get("keywords", [])

    prompt = f"""당신은 K뷰티 유튜브 쇼츠 전문 크리에이터입니다.
아래 트렌드 정보를 바탕으로 동남아 시청자를 타겟으로 한 60초짜리 쇼츠 영상 제작 데이터를 만들어주세요.

## 트렌드 정보
- 주제: {trend_topic}
- 관련 제품: {json.dumps(products, ensure_ascii=False)}
- 키워드: {json.dumps(keywords, ensure_ascii=False)}

## 영상 구성 규칙
- 총 길이: 60초 (나레이션 기준)
- 장면 수: 4~6개 (각 8~15초)
- 첫 3초: 반드시 강한 후킹 문구로 시작
- 톤: 친근하고 정보성 있는 뷰티 인플루언서 스타일
- 중간에 올리브영 제품 자연스럽게 언급

## 이미지 프롬프트 규칙 (중요)
- character_prompt: 반드시 단색 배경(plain solid-color background)으로 캐릭터만 생성
- location_prompt: 배경만 별도 생성 (인물 없이)
- action_prompt: Kling AI가 이해할 수 있는 간결한 영어 모션 설명
- 한국 여성 모델이 제품을 들거나 사용하는 자연스러운 장면
- 장소는 한국 감성: 카페, 편의점, 청계천, 한강, 올리브영 매장 등

## 출력 형식
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만 출력하세요.

{{
  "title": "영상 제목 (30자 이내, 한국어)",
  "hook_line": "첫 3초 후킹 문구 (10자 이내, 임팩트 있게)",
  "script": "전체 나레이션 스크립트 (200자 내외, 자연스러운 구어체)",
  "scenes": [
    {{
      "order": 1,
      "duration_sec": 10,
      "narration": "이 장면에서 읽히는 나레이션",
      "character_prompt": "영어로 작성된 캐릭터 이미지 프롬프트 (단색 배경 필수)",
      "location_prompt": "영어로 작성된 배경 이미지 프롬프트 (인물 없이)",
      "action_prompt": "영어로 작성된 Kling 모션 프롬프트",
      "product_placement": false
    }}
  ],
  "subtitles": {{
    "ko": "한국어 자막 (스크립트와 동일)",
    "en": "English translation",
    "th": "Thai translation",
    "vi": "Vietnamese translation",
    "id": "Indonesian translation"
  }}
}}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()

    # JSON 파싱 (```json 블록 제거)
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    result = json.loads(raw)

    # character_prompt에 기본 한국인 모델 구조 주입
    for scene in result.get("scenes", []):
        if scene.get("character_prompt"):
            scene["character_prompt"] = _inject_base_model_prompt(
                scene["character_prompt"]
            )

    return result


def _inject_base_model_prompt(scene_specific_prompt: str) -> str:
    """
    장면별 캐릭터 프롬프트에 기본 한국인 모델 구조를 결합.
    빠나나AI의 5단계 캐릭터 설정을 자동화하는 핵심 로직.
    """
    return f"{KOREAN_MODEL_BASE_PROMPT} {scene_specific_prompt}"


def generate_script_from_raw_post(raw_post: dict) -> dict:
    """
    인스타 원본 게시물(kbeauty_raw_posts)에서 직접 스크립트 생성.
    1단계 수집 봇이 Claude 분석을 건너뛴 경우 사용.
    """
    caption = raw_post.get("caption", "")
    hashtag = raw_post.get("hashtag", "")
    likes = raw_post.get("likes_count", 0)

    # 먼저 트렌드 분석
    analysis_prompt = f"""아래 인스타그램 게시물을 분석해서 K뷰티 트렌드 정보를 추출하세요.

캡션: {caption}
해시태그: {hashtag}
좋아요: {likes}

JSON으로만 응답:
{{
  "trend_topic": "트렌드 주제 요약",
  "products": ["제품1", "제품2"],
  "keywords": ["키워드1", "키워드2"],
  "relevance_score": 0.0
}}"""

    analysis_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": analysis_prompt}]
    )

    raw = analysis_response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    candidate = json.loads(raw.strip())
    return generate_shorts_script(candidate)


if __name__ == "__main__":
    # 테스트
    test_candidate = {
        "trend_topic": "마스크팩 매일 사용 전후 피부 변화",
        "products": ["메디힐 마스크팩", "SNP 마스크팩"],
        "keywords": ["마스크팩", "피부관리", "K뷰티루틴", "올리브영"],
    }

    print("스크립트 생성 중...")
    result = generate_shorts_script(test_candidate)
    print(json.dumps(result, ensure_ascii=False, indent=2))

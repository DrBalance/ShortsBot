"""
image_generator.py

Gemini Image API (나노바나나2)를 사용해서:
- 캐릭터 이미지 생성 (단색 배경, 제품 들고 있는 한국 여성)
- 로케이션 이미지 생성 (배경만, 인물 없이)
- 올리브영 제품 이미지 배경 합성

왼손AI 워크플로우:
  캐릭터 → 단색 배경으로 생성 → Kling에 넣어 모션화
  로케이션 → 별도 생성 → Kling에 참조로 전달
"""

import os
import base64
import httpx
import asyncio
from pathlib import Path
from typing import Optional
import google.generativeai as genai
from google.generativeai import types

# Gemini 설정
genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))

# 나노바나나2 = gemini-3.0-flash (이미지 생성)
# 나노바나나 Pro = gemini-3.0-pro (고품질, 한글 텍스트 지원)
IMAGE_MODEL_STANDARD = "gemini-2.0-flash-preview-image-generation"
IMAGE_MODEL_PRO = "gemini-2.0-flash-preview-image-generation"  # Pro 출시 시 교체


def generate_character_image(
    character_prompt: str,
    output_path: str,
    use_pro: bool = False,
) -> str:
    """
    캐릭터 이미지 생성.

    왼손AI 핵심 원칙:
    - 반드시 단색 배경 (plain solid-color background)
    - 영상 모델(Kling)이 캐릭터만 정확하게 참조할 수 있도록

    Args:
        character_prompt: script_generator에서 생성된 캐릭터 프롬프트
        output_path: 저장할 파일 경로 (.png)
        use_pro: Pro 모델 사용 여부 (한글 텍스트 필요 시)

    Returns:
        저장된 이미지 파일 경로
    """
    model_name = IMAGE_MODEL_PRO if use_pro else IMAGE_MODEL_STANDARD

    model = genai.GenerativeModel(model_name)

    response = model.generate_content(
        contents=character_prompt,
        generation_config=types.GenerationConfig(
            response_modalities=["IMAGE"],
            candidate_count=1,
        ),
    )

    image_data = None
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
            image_data = part.inline_data.data
            break

    if not image_data:
        raise ValueError("이미지 생성 실패: 응답에 이미지 없음")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if isinstance(image_data, str):
        image_bytes = base64.b64decode(image_data)
    else:
        image_bytes = image_data

    with open(output_path, "wb") as f:
        f.write(image_bytes)

    print(f"캐릭터 이미지 저장: {output_path}")
    return output_path


def generate_location_image(
    location_prompt: str,
    output_path: str,
) -> str:
    """
    로케이션(배경) 이미지 생성.

    왼손AI 원칙:
    - 로케이션은 나노바나나(Gemini) 추천 (GPT는 3D느낌)
    - 인물 없이 배경만 생성
    - 한국 감성 장소: 카페, 청계천, 올리브영 매장, 한강 등

    Args:
        location_prompt: 배경 설명 프롬프트
        output_path: 저장할 파일 경로

    Returns:
        저장된 이미지 파일 경로
    """
    # 로케이션에 항상 "인물 없음" 명시
    full_prompt = f"{location_prompt}, no people, no humans, empty scene, \
photorealistic, high detail, 4K quality, Korean aesthetic"

    model = genai.GenerativeModel(IMAGE_MODEL_STANDARD)

    response = model.generate_content(
        contents=full_prompt,
        generation_config=types.GenerationConfig(
            response_modalities=["IMAGE"],
            candidate_count=1,
        ),
    )

    image_data = None
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
            image_data = part.inline_data.data
            break

    if not image_data:
        raise ValueError("로케이션 이미지 생성 실패")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if isinstance(image_data, str):
        image_bytes = base64.b64decode(image_data)
    else:
        image_bytes = image_data

    with open(output_path, "wb") as f:
        f.write(image_bytes)

    print(f"로케이션 이미지 저장: {output_path}")
    return output_path


def edit_product_background(
    product_image_path: str,
    background_prompt: str,
    output_path: str,
) -> str:
    """
    올리브영 제품 이미지(흰 배경)를 감성적인 배경으로 합성.

    두 번째 문서에서 제안한 핵심 아이디어:
    "흰 배경 제품 이미지 → Gemini Image edit으로 감성 배경 합성"
    예: "감성적인 인스타 무드의 대리석 화장대 배경으로 변경해줘"

    Args:
        product_image_path: 올리브영 제품 원본 이미지 경로
        background_prompt: 변경할 배경 설명
        output_path: 저장할 파일 경로

    Returns:
        저장된 이미지 파일 경로
    """
    with open(product_image_path, "rb") as f:
        image_bytes = f.read()

    image_part = {
        "mime_type": "image/jpeg",
        "data": base64.b64encode(image_bytes).decode(),
    }

    edit_prompt = f"""이 제품 이미지의 배경을 바꿔주세요.
새로운 배경: {background_prompt}
제품 자체는 그대로 유지하고 배경만 변경하세요.
인스타그램 감성, 고품질, 자연스러운 합성"""

    model = genai.GenerativeModel(IMAGE_MODEL_STANDARD)

    response = model.generate_content(
        contents=[edit_prompt, image_part],
        generation_config=types.GenerationConfig(
            response_modalities=["IMAGE"],
            candidate_count=1,
        ),
    )

    image_data = None
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
            image_data = part.inline_data.data
            break

    if not image_data:
        raise ValueError("제품 배경 합성 실패")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if isinstance(image_data, str):
        image_bytes = base64.b64decode(image_data)
    else:
        image_bytes = image_data

    with open(output_path, "wb") as f:
        f.write(image_bytes)

    print(f"제품 배경 합성 완료: {output_path}")
    return output_path


def generate_scene_images(
    scenes: list,
    candidate_id: str,
    work_dir: str = "/tmp/kbeauty",
) -> list:
    """
    스크립트의 모든 장면에 대해 이미지 생성.

    Args:
        scenes: script_generator에서 생성된 scenes 리스트
        candidate_id: Supabase candidate ID (파일명 구분용)
        work_dir: 임시 작업 디렉토리

    Returns:
        scenes에 image_paths가 추가된 리스트
    """
    results = []

    for scene in scenes:
        order = scene["order"]
        print(f"\n장면 {order} 이미지 생성 중...")

        char_path = f"{work_dir}/{candidate_id}/scene_{order}_character.png"
        loc_path = f"{work_dir}/{candidate_id}/scene_{order}_location.png"

        try:
            generate_character_image(
                character_prompt=scene["character_prompt"],
                output_path=char_path,
            )
        except Exception as e:
            print(f"캐릭터 이미지 실패 (장면 {order}): {e}")
            char_path = None

        try:
            generate_location_image(
                location_prompt=scene["location_prompt"],
                output_path=loc_path,
            )
        except Exception as e:
            print(f"로케이션 이미지 실패 (장면 {order}): {e}")
            loc_path = None

        scene_with_paths = {
            **scene,
            "character_image_path": char_path,
            "location_image_path": loc_path,
        }
        results.append(scene_with_paths)

    return results


if __name__ == "__main__":
    # 테스트
    test_prompt = """A photorealistic portrait of a native South Korean (한국인) female model, \
mid 20s (24-26). natural warm skin tone, long straight silky hair. \
Facial features: large bright eyes with a slim V-line jawline. \
slim slender body. innocent pure and fresh aura. \
Korean makeup style (dewy glass skin, gradient lips, soft brow). \
NOT Korean-American or diaspora look. \
She is holding a white mask pack product, applying it to her face, surprised expression. \
plain solid-color light beige background, \
ultra-realistic, sharp focus, 8K quality."""

    print("테스트 이미지 생성 중...")
    generate_character_image(
        character_prompt=test_prompt,
        output_path="/tmp/test_character.png",
    )
    print("완료!")

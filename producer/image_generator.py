"""
image_generator.py

google.genai 패키지 (신버전)로 Gemini Image API 사용.
모델: gemini-3.1-flash-image (나노바나나2)

왼손AI 원칙:
- 캐릭터: 반드시 단색 배경으로 생성
- 로케이션: 인물 없이 배경만 생성
- 9:16 세로형 (쇼츠 비율)
"""

import os
import base64
from io import BytesIO
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from google import genai
from google.genai import types
from PIL import Image

# 나노바나나2 = gemini-3.1-flash-image
IMAGE_MODEL = "gemini-3.1-flash-image"

client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))


def generate_character_image(
    character_prompt: str,
    output_path: str,
    aspect_ratio: str = "9:16",
) -> str:
    """
    캐릭터 이미지 생성 (단색 배경 필수).

    Args:
        character_prompt: 캐릭터 프롬프트 (script_generator에서 생성)
        output_path: 저장할 .png 파일 경로
        aspect_ratio: 화면 비율 (쇼츠는 9:16)

    Returns:
        저장된 이미지 경로
    """
    response = client.models.generate_content(
        model=IMAGE_MODEL,
        contents=character_prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
        ),
    )

    return _save_image_from_response(response, output_path)


def generate_location_image(
    location_prompt: str,
    output_path: str,
    aspect_ratio: str = "9:16",
) -> str:
    """
    로케이션(배경) 이미지 생성 - 인물 없이.

    Args:
        location_prompt: 배경 프롬프트
        output_path: 저장할 .png 파일 경로
        aspect_ratio: 화면 비율

    Returns:
        저장된 이미지 경로
    """
    full_prompt = (
        f"{location_prompt}, "
        "no people, no humans, empty scene, "
        "photorealistic, high detail, 4K quality, Korean aesthetic"
    )

    response = client.models.generate_content(
        model=IMAGE_MODEL,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
        ),
    )

    return _save_image_from_response(response, output_path)


def edit_product_background(
    product_image_path: str,
    background_prompt: str,
    output_path: str,
) -> str:
    """
    올리브영 흰 배경 제품 이미지를 감성 배경으로 합성.

    Args:
        product_image_path: 원본 제품 이미지 경로
        background_prompt: 새 배경 설명
        output_path: 저장할 경로

    Returns:
        저장된 이미지 경로
    """
    product_image = Image.open(product_image_path)

    edit_prompt = (
        f"이 제품 이미지의 배경을 변경해주세요. "
        f"새로운 배경: {background_prompt}. "
        "제품 자체는 그대로 유지하고 배경만 바꿔주세요. "
        "인스타그램 감성, 고품질, 자연스러운 합성."
    )

    response = client.models.generate_content(
        model=IMAGE_MODEL,
        contents=[edit_prompt, product_image],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
        ),
    )

    return _save_image_from_response(response, output_path)


def _save_image_from_response(response, output_path: str) -> str:
    """응답에서 이미지 추출 후 저장"""
    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            image = Image.open(BytesIO(part.inline_data.data))
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            image.save(output_path)
            print(f"이미지 저장: {output_path} ({image.size[0]}x{image.size[1]})")
            return output_path

    raise ValueError("응답에 이미지 없음")


def generate_scene_images(
    scenes: list,
    candidate_id: str,
    work_dir: str = "/tmp/kbeauty",
) -> list:
    """모든 장면의 이미지 생성"""
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

        results.append({
            **scene,
            "character_image_path": char_path,
            "location_image_path": loc_path,
        })

    return results


if __name__ == "__main__":
    # 테스트 - 장면 1 캐릭터 이미지만 생성
    test_prompt = (
        "A photorealistic portrait of a native South Korean (한국인) female model, "
        "mid 20s (24-26). natural warm skin tone, long straight silky hair. "
        "Facial features: large bright eyes with a slim V-line jawline, delicate refined features. "
        "slim slender body. innocent pure and fresh aura. "
        "Korean makeup style (dewy glass skin, gradient lips, soft brow). "
        "NOT Korean-American or diaspora look. "
        "She is holding a white sheet mask package, excited expression. "
        "plain white solid-color background, "
        "ultra-realistic, sharp focus, 8K quality."
    )

    print("캐릭터 이미지 생성 중...")
    path = generate_character_image(
        character_prompt=test_prompt,
        output_path="/tmp/test_character.png",
        aspect_ratio="9:16",
    )
    print(f"완료: {path}")

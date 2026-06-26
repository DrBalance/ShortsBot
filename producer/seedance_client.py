"""
seedance_client.py

BytePlus ModelArk Seedance 2.0 API로 이미지 → 모션 영상 생성.

공식 API 엔드포인트:
  POST https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks
  GET  https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks/{id}

모델 ID:
  dreamina-seedance-2-0-260128        Standard (1080p 지원)
  dreamina-seedance-2-0-fast-260128   Fast (720p 한정)
  dreamina-seedance-2-0-mini-260615   Mini (720p 한정, 6월 25일 API 오픈 예정)

주의:
  공식 API는 실사 얼굴 이미지 직접 업로드를 제한함.
  - Seedance 자체 생성 영상의 출력물을 입력으로 재사용하는 방식은 허용
  - 공식 디지털 캐릭터 자산(asset://<ASSET_ID>) 사용은 허용
  - 왼손AI 등 서드파티 플랫폼은 이 제한이 완화되어 있을 수 있음
"""

import os
import time
import httpx
import base64
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ─── 상수 ────────────────────────────────────────────────────────────────────

BYTEPLUS_BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"
TASKS_ENDPOINT    = f"{BYTEPLUS_BASE_URL}/contents/generations/tasks"

# 모델 ID (교체만 하면 됨)
MODEL_STANDARD = "dreamina-seedance-2-0-260128"
MODEL_FAST     = "dreamina-seedance-2-0-fast-260128"
MODEL_MINI     = "dreamina-seedance-2-0-mini-260615"  # 6월 25일 API 오픈 예정


# ─── 인증 헤더 ───────────────────────────────────────────────────────────────

def _get_headers() -> dict:
    """BytePlus ModelArk API 요청 헤더 반환"""
    api_key = os.environ.get("ARK_API_KEY")
    if not api_key:
        raise ValueError("ARK_API_KEY 환경변수가 없습니다")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


# ─── 이미지 → base64 ─────────────────────────────────────────────────────────

def _image_to_base64_url(image_path: str) -> str:
    """
    이미지 파일을 API 형식의 base64 문자열로 변환.
    형식: data:image/<ext>;base64,<data>
    """
    path = Path(image_path)
    ext = path.suffix.lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    return f"data:image/{ext};base64,{b64}"


# ─── 핵심 함수 ───────────────────────────────────────────────────────────────

def create_image_to_video(
    image_path: str,
    action_prompt: str,
    output_path: str,
    duration: int = 5,
    aspect_ratio: str = "9:16",
    resolution: str = "720p",
    model: str = MODEL_MINI,
    generate_audio: bool = False,
) -> str:
    """
    이미지 → 모션 영상 생성 (Seedance Image to Video, first frame 방식).

    Args:
        image_path:     캐릭터/액터 이미지 경로
        action_prompt:  모션 설명 프롬프트 (영어 권장)
        output_path:    저장할 .mp4 경로
        duration:       영상 길이 초 (4~15, Seedance 2.0 기준)
        aspect_ratio:   화면 비율 (쇼츠는 9:16)
        resolution:     해상도 (480p / 720p / 1080p — Mini는 720p까지)
        model:          사용할 모델 ID
        generate_audio: True면 AI 오디오 자동 생성 (TTS 별도 사용 시 False)

    Returns:
        저장된 영상 파일 경로
    """
    image_b64 = _image_to_base64_url(image_path)

    payload = {
        "model": model,
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": image_b64},
                "role": "first_frame",
            },
            {
                "type": "text",
                "text": action_prompt,
            },
        ],
        "resolution": resolution,
        "ratio": aspect_ratio,
        "duration": duration,
        "generate_audio": generate_audio,
        "watermark": False,
    }

    print(f"  [Seedance] 영상 생성 요청: model={model}, {resolution} {aspect_ratio} {duration}s")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            TASKS_ENDPOINT,
            headers=_get_headers(),
            json=payload,
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Seedance API 요청 실패 {response.status_code}: {response.text}"
        )

    task_data = response.json()
    task_id = task_data.get("id")

    if not task_id:
        raise RuntimeError(f"task_id 없음: {task_data}")

    print(f"  [Seedance] 영상 생성 시작: task_id={task_id}")

    video_url = _poll_video_task(task_id)
    _download_video(video_url, output_path)

    print(f"  [Seedance] 영상 저장 완료: {output_path}")
    return output_path


def create_reference_to_video(
    actor_image_path: str,
    action_prompt: str,
    output_path: str,
    extra_image_paths: Optional[list] = None,
    duration: int = 5,
    aspect_ratio: str = "9:16",
    resolution: str = "720p",
    model: str = MODEL_MINI,
    generate_audio: bool = False,
) -> str:
    """
    멀티모달 레퍼런스 방식으로 영상 생성 (액터 고정 방식).

    액터 이미지를 reference_image role로 투입 → 캐릭터 일관성 향상.
    추가 이미지(제품 등)도 함께 넣을 수 있음.

    Args:
        actor_image_path:   액터 이미지 경로 (인물 라이브러리 기준)
        action_prompt:      모션 설명 + @image1 참조 구문 포함 프롬프트
        output_path:        저장할 .mp4 경로
        extra_image_paths:  추가 레퍼런스 이미지 경로 목록 (제품 이미지 등)
        duration:           영상 길이 초
        aspect_ratio:       화면 비율
        resolution:         해상도
        model:              모델 ID
        generate_audio:     AI 오디오 생성 여부

    Returns:
        저장된 영상 파일 경로

    Note:
        공식 API에서 실사 얼굴 이미지는 제한될 수 있음.
        Seedance 자체 생성 영상 출력물이나 공식 디지털 캐릭터 자산만 허용.
    """
    content = []

    # 액터 이미지 (reference_image)
    actor_b64 = _image_to_base64_url(actor_image_path)
    content.append({
        "type": "image_url",
        "image_url": {"url": actor_b64},
        "role": "reference_image",
    })

    # 추가 이미지들 (제품 이미지 등)
    if extra_image_paths:
        for extra_path in extra_image_paths:
            if Path(extra_path).exists():
                extra_b64 = _image_to_base64_url(extra_path)
                content.append({
                    "type": "image_url",
                    "image_url": {"url": extra_b64},
                    "role": "reference_image",
                })

    # 텍스트 프롬프트
    content.append({
        "type": "text",
        "text": action_prompt,
    })

    payload = {
        "model": model,
        "content": content,
        "resolution": resolution,
        "ratio": aspect_ratio,
        "duration": duration,
        "generate_audio": generate_audio,
        "watermark": False,
    }

    print(f"  [Seedance] 레퍼런스 영상 생성 요청: {len(content)-1}개 이미지, {resolution} {aspect_ratio} {duration}s")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            TASKS_ENDPOINT,
            headers=_get_headers(),
            json=payload,
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Seedance API 요청 실패 {response.status_code}: {response.text}"
        )

    task_data = response.json()
    task_id = task_data.get("id")

    if not task_id:
        raise RuntimeError(f"task_id 없음: {task_data}")

    print(f"  [Seedance] 레퍼런스 영상 생성 시작: task_id={task_id}")

    video_url = _poll_video_task(task_id)
    _download_video(video_url, output_path)

    print(f"  [Seedance] 영상 저장 완료: {output_path}")
    return output_path


# ─── 폴링 ────────────────────────────────────────────────────────────────────

def _poll_video_task(
    task_id: str,
    max_wait_sec: int = 300,
    interval_sec: int = 10,
) -> str:
    """Seedance 영상 생성 완료까지 폴링"""
    elapsed = 0

    while elapsed < max_wait_sec:
        time.sleep(interval_sec)
        elapsed += interval_sec

        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{TASKS_ENDPOINT}/{task_id}",
                headers=_get_headers(),
            )

        if response.status_code != 200:
            print(f"  폴링 오류 {response.status_code}, 재시도...")
            continue

        data = response.json()
        status = data.get("status")
        print(f"  [Seedance] 상태: {status} ({elapsed}초 경과)")

        if status == "succeeded":
            video_url = data.get("content", {}).get("video_url")
            if video_url:
                # 토큰 사용량 출력 (비용 추적용)
                usage = data.get("usage", {})
                tokens = usage.get("completion_tokens", "?")
                print(f"  [Seedance] 완료 — 사용 토큰: {tokens}")
                return video_url
            raise RuntimeError("video_url 없음")

        elif status == "failed":
            error = data.get("error", {})
            raise RuntimeError(
                f"Seedance 영상 생성 실패: {error.get('code')} — {error.get('message')}"
            )

        elif status == "expired":
            raise TimeoutError("Seedance 태스크 만료 (expired)")

    raise TimeoutError(f"Seedance 타임아웃: {max_wait_sec}초 초과")


# ─── 다운로드 ─────────────────────────────────────────────────────────────────

def _download_video(url: str, output_path: str) -> None:
    """영상 URL에서 파일 다운로드 (24시간 내 저장 필수)"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=120.0) as client:
        response = client.get(url)

    if response.status_code != 200:
        raise RuntimeError(f"영상 다운로드 실패: {response.status_code}")

    with open(output_path, "wb") as f:
        f.write(response.content)


# ─── 파이프라인용 배치 함수 ───────────────────────────────────────────────────

def generate_scene_videos(
    scenes: list,
    candidate_id: str,
    actor_image_path: Optional[str] = None,
    work_dir: str = "/tmp/kbeauty",
    model: str = MODEL_MINI,
) -> list:
    """
    모든 장면의 모션 영상 생성.

    actor_image_path가 지정되면 reference_to_video 방식 (액터 고정),
    없으면 first_frame 방식 (장면별 캐릭터 이미지 사용).
    """
    results = []

    for scene in scenes:
        order = scene["order"]
        char_path = scene.get("character_image_path")
        action_prompt = scene.get("action_prompt", "")
        product_paths = scene.get("product_image_paths", [])

        video_path = f"{work_dir}/{candidate_id}/scene_{order}_video.mp4"

        try:
            if actor_image_path and Path(actor_image_path).exists():
                # 액터 고정 방식
                create_reference_to_video(
                    actor_image_path=actor_image_path,
                    action_prompt=action_prompt,
                    output_path=video_path,
                    extra_image_paths=product_paths,
                    duration=5,
                    aspect_ratio="9:16",
                    resolution="720p",
                    model=model,
                )
            elif char_path and Path(char_path).exists():
                # 장면별 이미지 방식 (fallback)
                create_image_to_video(
                    image_path=char_path,
                    action_prompt=action_prompt,
                    output_path=video_path,
                    duration=5,
                    aspect_ratio="9:16",
                    resolution="720p",
                    model=model,
                )
            else:
                print(f"장면 {order}: 이미지 없음, 스킵")
                results.append({**scene, "video_path": None})
                continue

            results.append({**scene, "video_path": video_path})

        except Exception as e:
            print(f"장면 {order} 영상 생성 실패: {e}")
            results.append({**scene, "video_path": None})

    return results


# ─── 테스트 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    test_image = sys.argv[1] if len(sys.argv) > 1 else "/tmp/test_actor.png"
    output = "/tmp/test_seedance_video.mp4"

    if not Path(test_image).exists():
        print(f"테스트 이미지 없음: {test_image}")
        print("사용법: python seedance_client.py <이미지경로>")
        sys.exit(1)

    print(f"Seedance 2.0 Mini 영상 생성 테스트: {test_image}")
    print(f"주의: 공식 API에서 실사 얼굴 이미지는 제한될 수 있음")

    try:
        create_image_to_video(
            image_path=test_image,
            action_prompt=(
                "A young Korean woman holds up a sheet mask package toward camera, "
                "smiles brightly and nods with excitement, "
                "soft natural movement, beauty influencer style, "
                "clean pastel background"
            ),
            output_path=output,
            duration=5,
            aspect_ratio="9:16",
            resolution="720p",
            model=MODEL_MINI,
            generate_audio=False,
        )
        print(f"완료: {output}")
    except Exception as e:
        print(f"실패: {e}")

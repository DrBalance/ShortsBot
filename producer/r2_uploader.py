"""
r2_uploader.py

Cloudflare R2에 완성 영상 및 자막 파일 업로드.
boto3 S3 호환 API 사용.
"""

import os
import boto3
from pathlib import Path
from botocore.config import Config
from dotenv import load_dotenv
load_dotenv()


def get_r2_client():
    """Cloudflare R2 클라이언트 반환"""
    account_id = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")

    if not all([account_id, access_key, secret_key]):
        raise ValueError("R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY 필요")

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def upload_video(
    local_path: str,
    candidate_id: str,
    bucket: str = None,
) -> dict:
    """
    완성 영상을 R2에 업로드.

    저장 경로 구조:
    videos/{candidate_id}/final.mp4

    Args:
        local_path: 로컬 MP4 파일 경로
        candidate_id: Supabase candidate ID
        bucket: R2 버킷명 (없으면 환경변수)

    Returns:
        {"r2_key": "...", "public_url": "..."}
    """
    bucket = bucket or os.environ.get("R2_BUCKET_NAME", "kbeauty-shorts")
    r2_key = f"videos/{candidate_id}/final.mp4"

    client = get_r2_client()

    file_size = Path(local_path).stat().st_size
    print(f"R2 업로드 중: {r2_key} ({file_size / (1024*1024):.1f}MB)")

    with open(local_path, "rb") as f:
        client.upload_fileobj(
            f,
            bucket,
            r2_key,
            ExtraArgs={
                "ContentType": "video/mp4",
                "CacheControl": "public, max-age=31536000",
            },
        )

    # 공개 URL (R2 Public Bucket 설정 필요)
    account_id = os.environ.get("R2_ACCOUNT_ID")
    public_url = f"https://pub-{account_id}.r2.dev/{r2_key}"

    print(f"R2 업로드 완료: {public_url}")
    return {
        "r2_key": r2_key,
        "video_url": public_url,
    }


def upload_subtitles(
    srt_paths: dict,
    candidate_id: str,
    bucket: str = None,
) -> dict:
    """
    다국어 SRT 자막 파일들을 R2에 업로드.

    저장 경로: videos/{candidate_id}/subtitle_{lang}.srt

    Args:
        srt_paths: {"ko": "경로", "en": "경로", ...}
        candidate_id: candidate ID
        bucket: R2 버킷명

    Returns:
        {"ko": "r2_key", "en": "r2_key", ...}
    """
    bucket = bucket or os.environ.get("R2_BUCKET_NAME", "kbeauty-shorts")
    client = get_r2_client()
    uploaded = {}

    for lang, local_path in srt_paths.items():
        if not local_path or not Path(local_path).exists():
            continue

        r2_key = f"videos/{candidate_id}/subtitle_{lang}.srt"

        with open(local_path, "rb") as f:
            client.upload_fileobj(
                f,
                bucket,
                r2_key,
                ExtraArgs={"ContentType": "text/plain; charset=utf-8"},
            )

        uploaded[lang] = r2_key
        print(f"자막 업로드 ({lang}): {r2_key}")

    return uploaded

if __name__ == "__main__":
    print("R2 업로드 테스트 중...")
    result = upload_video(
        local_path="/tmp/test_kling_video.mp4",
        candidate_id="test_001",
    )
    print(f"완료: {result}")
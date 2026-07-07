"""
music_add.py
Thematic에서 다운받은 음악 파일을 music/raw/ 폴더로 이동하고
메타데이터를 music/raw/{stem}.json으로 저장하는 CLI 도구.

사용법:
    python music_add.py
    python music_add.py --file ~/Downloads/sunday_morning.mp3  # 파일 경로 미리 지정

이후 music_preprocessor.py를 실행하면 beat_sync → R2 업로드 → Supabase 저장까지 처리됨.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# music/raw/ 폴더 위치 (이 스크립트 기준 상위 디렉터리)
MUSIC_RAW_DIR = Path(__file__).parent / "music" / "raw"

MOOD_OPTIONS = ["upbeat", "chill", "dreamy", "energetic", "romantic", "mysterious"]


def prompt(label: str, required: bool = True, default: str = "") -> str:
    """사용자 입력을 받는 헬퍼. required=True이면 빈 입력을 허용하지 않는다."""
    while True:
        hint = f" [{default}]" if default else ""
        value = input(f"{label}{hint}: ").strip()
        if not value and default:
            return default
        if value:
            return value
        if not required:
            return ""
        print("  ⚠️  필수 항목입니다. 다시 입력해주세요.")


def pick_mood() -> str:
    """mood를 번호로 선택."""
    print("\nMood 선택:")
    for i, m in enumerate(MOOD_OPTIONS, 1):
        print(f"  {i}. {m}")
    while True:
        raw = input("번호 입력: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(MOOD_OPTIONS):
            return MOOD_OPTIONS[int(raw) - 1]
        print(f"  ⚠️  1~{len(MOOD_OPTIONS)} 사이의 번호를 입력해주세요.")


def add_track(file_path: Path | None) -> None:
    MUSIC_RAW_DIR.mkdir(parents=True, exist_ok=True)

    # 파일 경로 확인
    if file_path is None:
        raw = input("mp3 파일 경로 (드래그 앤 드롭 가능): ").strip().strip("'\"")
        file_path = Path(raw).expanduser().resolve()

    if not file_path.exists():
        print(f"❌ 파일을 찾을 수 없습니다: {file_path}")
        sys.exit(1)
    if file_path.suffix.lower() not in (".mp3", ".wav", ".flac", ".m4a"):
        print(f"❌ 지원하지 않는 파일 형식입니다: {file_path.suffix}")
        sys.exit(1)

    print(f"\n📂 파일: {file_path.name}")
    print("─" * 50)

    # 메타데이터 입력
    title = prompt("곡명")
    artist = prompt("아티스트")
    thematic_url = prompt("Thematic URL (설명란 크레딧용)")
    mood = pick_mood()

    # 대상 경로
    dest_mp3 = MUSIC_RAW_DIR / file_path.name
    dest_json = MUSIC_RAW_DIR / (file_path.stem + ".json")

    # 중복 확인
    if dest_mp3.exists():
        overwrite = input(f"\n⚠️  {dest_mp3.name}이 이미 존재합니다. 덮어쓰시겠습니까? (y/N): ").strip().lower()
        if overwrite != "y":
            print("취소되었습니다.")
            sys.exit(0)

    # 파일 이동
    shutil.copy2(file_path, dest_mp3)

    # JSON 저장
    meta = {
        "title": title,
        "artist": artist,
        "thematic_url": thematic_url,
        "mood": mood,
        "filename": file_path.name,
    }
    dest_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅ 완료!")
    print(f"   음악 파일 → {dest_mp3}")
    print(f"   메타데이터 → {dest_json}")
    print(f"\n다음 단계: python music_preprocessor.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="Thematic 음악 파일 등록 CLI")
    parser.add_argument("--file", type=str, default=None, help="mp3 파일 경로 (생략 시 대화형 입력)")
    args = parser.parse_args()

    file_path = Path(args.file).expanduser().resolve() if args.file else None

    print("=" * 50)
    print("  ShortsBot 음악 등록")
    print("=" * 50)
    add_track(file_path)


if __name__ == "__main__":
    main()

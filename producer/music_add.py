"""
music_add.py
music/raw/ 폴더의 mp3 파일들의 Finder "설명"(kMDItemFinderComment)에서
Thematic 크레딧 문구를 자동으로 읽어와 메타데이터 JSON을 생성하는 CLI 도구.

사전 준비:
    Thematic에서 다운로드 후, Finder "정보 가져오기" (Cmd+I) → 설명란에
    크레딧 문구를 붙여넣어 둘 것.
    예) Free Music for Videos 👉 Music by Topogy - Thief Of My Heart - https://thmatc.co/?l=D6D6983F

사용법:
    1. mp3 파일들을 producer/music/raw/ 폴더에 복사 (Finder 설명 작성 완료 상태)
    2. python music_add.py 실행
    3. 자동 파싱 결과 확인, mood만 선택 (파싱 실패 시 수동 입력)
    4. 완료 후 python music_preprocessor.py 실행
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

MUSIC_RAW_DIR = Path(__file__).parent / "music" / "raw"

MOOD_OPTIONS = ["upbeat", "chill", "dreamy", "energetic", "romantic", "mysterious"]
MOOD_UNKNOWN = "unknown"

# "Music by {artist} - {title} - {url}" 패턴 파싱
ATTRIBUTION_PATTERN = re.compile(
    r"Music by\s+(?P<artist>.+?)\s*-\s*(?P<title>.+?)\s*-\s*(?P<url>https?://\S+)"
)


def get_finder_comment(file_path: Path) -> str:
    """
    AppleScript(osascript)로 Finder의 comment 속성을 직접 읽어온다.
    xattr/mdls는 Spotlight 인덱싱 지연이나 캡처 방식 문제로 값이 깨지는 경우가 있어,
    Finder 앱에 직접 물어보는 이 방식이 가장 안정적으로 확인됨.
    """
    script = f'''
        set theFile to POSIX file "{file_path}" as alias
        tell application "Finder"
            return comment of theFile
        end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def parse_attribution(comment: str) -> dict:
    """크레딧 문구에서 artist/title/url 파싱. 실패 시 빈 값."""
    match = ATTRIBUTION_PATTERN.search(comment)
    if match:
        return {
            "artist": match.group("artist").strip(),
            "title": match.group("title").strip(),
            "url": match.group("url").strip(),
        }
    return {"artist": "", "title": "", "url": ""}


def prompt(label: str, required: bool = True, default: str = "") -> str:
    """사용자 입력 헬퍼. required=True이면 빈 입력 불허."""
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
    """mood를 번호로 선택. Enter만 치면 'unknown'으로 저장(나중에 채우면 됨)."""
    print(f"Mood 선택 (Enter만 치면 '{MOOD_UNKNOWN}'로 저장):")
    for i, m in enumerate(MOOD_OPTIONS, 1):
        print(f"  {i}. {m}")
    while True:
        raw = input("번호 입력: ").strip()
        if raw == "":
            print(f"  ❓ '{MOOD_UNKNOWN}'로 저장됩니다.")
            return MOOD_UNKNOWN
        if raw.isdigit() and 1 <= int(raw) <= len(MOOD_OPTIONS):
            return MOOD_OPTIONS[int(raw) - 1]
        print(f"  ⚠️  1~{len(MOOD_OPTIONS)} 사이의 번호를 입력하거나 Enter로 건너뛰세요.")


def process_track(mp3_path: Path) -> bool:
    """
    단일 mp3 파일 처리. Finder 설명에서 attribution 자동 파싱 시도,
    실패 시 수동 입력으로 보완. JSON 저장 후 True/False 반환.
    """
    json_path = mp3_path.parent / (mp3_path.stem + ".json")
    if json_path.exists():
        print(f"⏭️  이미 메타데이터 있음, 건너뜀: {mp3_path.name}")
        return False

    print(f"\n{'─' * 50}")
    print(f"📂 {mp3_path.name}")
    print(f"{'─' * 50}")

    comment = get_finder_comment(mp3_path)

    if not comment:
        print("  ⚠️  Finder 설명이 비어 있습니다. Cmd+I로 크레딧 문구를 먼저 붙여넣어 주세요.")
        skip = input("  지금 수동으로 입력하시겠습니까? (Y/n): ").strip().lower()
        if skip == "n":
            print("  건너뜀.")
            return False
        comment = prompt("크레딧 문구 전체 (Free Music for Videos... 형태)")

    print(f"  📋 크레딧: {comment}")

    parsed = parse_attribution(comment)
    if parsed["title"] and parsed["artist"]:
        print(f"  ✅ 자동 파싱 → 아티스트: {parsed['artist']} / 곡명: {parsed['title']}")
        confirm = input("  이대로 저장할까요? (Y/n): ").strip().lower()
        if confirm == "n":
            parsed["artist"] = prompt("아티스트", default=parsed["artist"])
            parsed["title"] = prompt("곡명", default=parsed["title"])
    else:
        print("  ⚠️  자동 파싱 실패. 수동으로 입력해주세요.")
        parsed["artist"] = prompt("아티스트")
        parsed["title"] = prompt("곡명")

    mood = pick_mood()

    meta = {
        "title": parsed["title"],
        "artist": parsed["artist"],
        "attribution": comment,
        "thematic_url": parsed["url"],
        "mood": mood,
        "filename": mp3_path.name,
    }
    json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✅ 저장: {json_path.name}")
    return True


def main() -> None:
    print("=" * 50)
    print("  ShortsBot 음악 메타데이터 일괄 입력 (Finder 설명 자동 인식)")
    print("=" * 50)

    if not MUSIC_RAW_DIR.exists():
        print(f"❌ 폴더가 없습니다: {MUSIC_RAW_DIR}")
        print("   mp3 파일을 producer/music/raw/ 폴더에 넣고 다시 실행하세요.")
        sys.exit(1)

    mp3_files = sorted(MUSIC_RAW_DIR.glob("*.mp3"))
    if not mp3_files:
        print("❌ mp3 파일이 없습니다.")
        sys.exit(1)

    pending = [f for f in mp3_files if not (f.parent / (f.stem + ".json")).exists()]

    if not pending:
        print("✅ 모든 파일에 메타데이터가 이미 있습니다.")
        print("   다음 단계: python music_preprocessor.py")
        sys.exit(0)

    print(f"\n처리 대상: {len(pending)}개 파일 (전체 {len(mp3_files)}개 중)\n")

    done = 0
    for i, mp3_path in enumerate(pending, 1):
        print(f"\n[{i}/{len(pending)}]", end="")
        if process_track(mp3_path):
            done += 1

    print(f"\n{'=' * 50}")
    print(f"완료: {done}개 파일 메타데이터 저장")
    print(f"다음 단계: python music_preprocessor.py")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()

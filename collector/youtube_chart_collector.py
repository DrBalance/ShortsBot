"""
youtube_chart_collector.py
charts.youtube.com의 비공식 내부 API(youtubei/v1/browse)를 호출해
YouTube 공식 "Top Songs on Shorts" 차트를 국가별로 가져옵니다.

인증 불필요 확인됨 (2026-07-02, 시크릿 모드 캡처 기반):
  - charts.youtube.com 메인 페이지를 한 번 GET하면 익명 세션 쿠키
    (VISITOR_INFO1_LIVE, YSC 등)가 자동 발급됨
  - 그 쿠키로 /youtubei/v1/browse?alt=json에 POST하면 로그인 없이 응답 옴

주의:
  - 이 모듈은 Anthropic 샌드박스 네트워크에서는 charts.youtube.com이
    egress allowlist에 없어 테스트 불가. 로컬(Mac Studio)에서 실행해서
    검증 필요.
  - 실행 후 raw 응답 JSON을 저장하도록 했으니, 최초 실행 시 그 결과를
    Claude에게 다시 공유하면 parse_chart_response()를 실제 구조에 맞게
    완성함 (현재는 추정 구조라 방어적으로 여러 경로를 시도함).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict

import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

BROWSE_URL = "https://charts.youtube.com/youtubei/v1/browse?alt=json"

# 확인된 국가 코드 (호주는 Shorts 전용 차트 없음 — TODO.md 기록 반영)
SUPPORTED_COUNTRIES = ["us", "gb", "ca"]

PERIOD_TYPES = {"weekly": "WEEKLY", "daily": "DAILY"}


@dataclass
class ChartTrack:
    rank: int
    title: str
    artists: list[str]
    video_id: str | None = None          # encryptedVideoId — 실제 유튜브 video ID
    alt_video_id: str | None = None      # atvExternalVideoId — 대체/원곡 video ID
    previous_rank: int | None = None
    percent_views_change: float | None = None
    periods_on_chart: int | None = None
    label: str | None = None             # sublabel (배급사/레이블)
    country_code: str | None = None
    period_type: str | None = None
    end_date: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _build_session() -> requests.Session:
    """익명 세션 쿠키를 확보한 requests.Session을 반환."""
    session = requests.Session()
    session.headers.update({"user-agent": USER_AGENT, "accept-language": "en-US,en;q=0.9"})

    resp = session.get(
        "https://charts.youtube.com/charts/TopShortsSongs/us/weekly",
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"charts.youtube.com 메인 페이지 GET 실패 (status={resp.status_code}). "
            "차단되었거나 URL 구조가 바뀌었을 수 있습니다."
        )
    logger.info(f"세션 쿠키 확보 완료: {list(session.cookies.get_dict().keys())}")
    return session


def fetch_shorts_chart_raw(
    session: requests.Session,
    country_code: str,
    period: str = "weekly",
) -> dict:
    """
    지정 국가의 Shorts 차트 원본 JSON 응답을 반환.

    Args:
        session: _build_session()으로 만든 세션 (쿠키 보유)
        country_code: "us" / "gb" / "ca"
        period: "weekly" 또는 "daily"
    """
    if country_code not in SUPPORTED_COUNTRIES:
        logger.warning(
            f"'{country_code}'는 확인된 지원 국가 목록({SUPPORTED_COUNTRIES})에 없습니다. "
            "시도는 하되 결과가 비어있을 수 있습니다."
        )
    period_param = PERIOD_TYPES.get(period, "WEEKLY")

    headers = {
        "content-type": "application/json",
        "origin": "https://charts.youtube.com",
        "referer": f"https://charts.youtube.com/charts/TopShortsSongs/{country_code}/{period}",
        "x-youtube-client-name": "31",
        "x-youtube-client-version": "2.0",
        "accept": "*/*",
    }

    body = {
        "context": {
            "client": {
                "clientName": "WEB_MUSIC_ANALYTICS",
                "clientVersion": "2.0",
                "hl": "en",
                "gl": country_code.upper(),
                "experimentIds": [],
                "experimentsToken": "",
                "theme": "MUSIC",
            },
            "capabilities": {},
            "request": {"internalExperimentFlags": []},
        },
        "browseId": "FEmusic_analytics_charts_home",
        "query": (
            "flags=MusicCharts__enable_apac_and_shorts_charts_expansion"
            "&perspective=CHART_DETAILS"
            f"&chart_params_country_code={country_code}"
            "&chart_params_chart_type=SHORTS_TRACKS_BY_USAGE"
            f"&chart_params_period_type={period_param}"
        ),
    }

    resp = session.post(BROWSE_URL, headers=headers, json=body, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(
            f"차트 API 호출 실패 (status={resp.status_code}): {resp.text[:300]}"
        )
    return resp.json()


def parse_chart_response(raw: dict) -> list[ChartTrack]:
    """
    원본 JSON에서 곡 목록을 추출.

    실제 응답 구조 (2026-07-02, us/gb weekly 응답으로 검증됨):

    contents.sectionListRenderer.contents[0].musicAnalyticsSectionRenderer
      .content.trackTypes[0]
        .listType         # 예: "TOP_SHORTS_BY_USAGE"
        .chartPeriodType   # 예: "CHART_PERIOD_TYPE_WEEKLY"
        .endDate           # 예: "2026-06-25"
        .trackViews[]      # 곡 목록 (요청 하나당 50곡)
            .name                          # 곡 제목
            .artists[].name                # 아티스트명 리스트
            .encryptedVideoId              # 실제 유튜브 video ID (숏폼 재생용)
            .atvExternalVideoId            # 대체 video ID (원곡/뮤비인 경우가 많음)
            .sublabel                      # 배급사/레이블
            .chartEntryMetadata
                .currentPosition
                .previousPosition
                .percentViewsChange
                .periodsOnChart

    country_params.chartParams에서 country_code도 함께 회수해 메타데이터로 첨부.
    """
    tracks: list[ChartTrack] = []

    try:
        section = raw["contents"]["sectionListRenderer"]["contents"][0][
            "musicAnalyticsSectionRenderer"
        ]["content"]
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"응답 최상위 구조가 예상과 다릅니다: {e}")
        return tracks

    country_code = None
    try:
        country_code = section["perspectiveMetadata"]["requestParams"]["chartParams"][
            "countryCode"
        ]
    except (KeyError, TypeError):
        pass

    track_types = section.get("trackTypes", [])
    if not track_types:
        logger.error("trackTypes가 비어있습니다.")
        return tracks

    track_type = track_types[0]
    period_type = track_type.get("chartPeriodType")
    end_date = track_type.get("endDate")
    track_views = track_type.get("trackViews", [])

    for item in track_views:
        try:
            meta = item.get("chartEntryMetadata", {})
            artists = [a.get("name") for a in item.get("artists", []) if a.get("name")]

            tracks.append(
                ChartTrack(
                    rank=meta.get("currentPosition", len(tracks) + 1),
                    title=item.get("name", "UNKNOWN"),
                    artists=artists,
                    video_id=item.get("encryptedVideoId"),
                    alt_video_id=item.get("atvExternalVideoId"),
                    previous_rank=meta.get("previousPosition"),
                    percent_views_change=meta.get("percentViewsChange"),
                    periods_on_chart=meta.get("periodsOnChart"),
                    label=item.get("sublabel"),
                    country_code=country_code,
                    period_type=period_type,
                    end_date=end_date,
                )
            )
        except Exception as e:
            logger.warning(f"항목 파싱 중 오류, 건너뜀: {e}")

    tracks.sort(key=lambda t: t.rank)
    return tracks


def collect_all_shorts_charts(period: str = "weekly", save_raw_dir: str | None = None) -> dict:
    """
    SUPPORTED_COUNTRIES 전체에 대해 Shorts 차트를 수집.

    Returns:
        {country_code: [ChartTrack, ...]}
    """
    session = _build_session()
    results = {}

    for country in SUPPORTED_COUNTRIES:
        logger.info(f"[{country}] 차트 수집 시작")
        raw = fetch_shorts_chart_raw(session, country, period=period)

        if save_raw_dir:
            path = f"{save_raw_dir}/chart_raw_{country}_{period}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            logger.info(f"원본 응답 저장: {path}")

        tracks = parse_chart_response(raw)
        results[country] = tracks
        logger.info(f"[{country}] {len(tracks)}곡 파싱됨")

        time.sleep(1.5)  # 과도한 연속 호출 방지

    return results


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    period = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    save_dir = sys.argv[2] if len(sys.argv) > 2 else "."

    results = collect_all_shorts_charts(period=period, save_raw_dir=save_dir)

    for country, tracks in results.items():
        print(f"\n=== {country.upper()} ({period}) ===")
        for t in tracks[:10]:
            artist_str = ", ".join(t.artists) if t.artists else "Unknown"
            print(f"{t.rank}. {t.title} — {artist_str} (videoId={t.video_id}, prev={t.previous_rank})")

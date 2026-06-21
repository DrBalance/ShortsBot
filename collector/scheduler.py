"""
collector/scheduler.py
APScheduler를 사용해 수집 봇과 분석 봇을 주기적으로 실행합니다.
"""
import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from collector.apify_scraper import run_collection
from collector.claude_analyzer import run_analysis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def job_collect():
    """수집 Job: Apify로 인스타 게시물 수집."""
    logger.info("=" * 50)
    logger.info("수집 Job 시작")
    try:
        count = run_collection()
        logger.info(f"수집 Job 완료: 신규 {count}건")
    except Exception as e:
        logger.error(f"수집 Job 오류: {e}", exc_info=True)


def job_analyze():
    """분석 Job: Claude로 트렌드 소재 추출."""
    logger.info("=" * 50)
    logger.info("분석 Job 시작")
    try:
        count = run_analysis(batch_size=config.MAX_ITEMS_PER_RUN)
        logger.info(f"분석 Job 완료: 후보 {count}건 저장")
    except Exception as e:
        logger.error(f"분석 Job 오류: {e}", exc_info=True)


def start():
    """스케줄러 시작 (블로킹)."""
    # 환경변수 체크
    missing = config.validate()
    if missing:
        logger.warning(f"누락된 환경변수: {missing} — 해당 기능은 실행 시 에러 발생")

    scheduler = BlockingScheduler(timezone="Asia/Seoul")

    # 수집: config.COLLECT_INTERVAL_HOURS 마다
    scheduler.add_job(
        job_collect,
        trigger=IntervalTrigger(hours=config.COLLECT_INTERVAL_HOURS),
        id="collect",
        name="Apify 수집 봇",
        replace_existing=True,
        max_instances=1,
    )

    # 분석: 수집 주기 절반마다 (수집 후 빠르게 분석)
    scheduler.add_job(
        job_analyze,
        trigger=IntervalTrigger(hours=max(1, config.COLLECT_INTERVAL_HOURS // 2)),
        id="analyze",
        name="Claude 분석 봇",
        replace_existing=True,
        max_instances=1,
    )

    logger.info(
        f"스케줄러 시작 — "
        f"수집: {config.COLLECT_INTERVAL_HOURS}시간마다 | "
        f"분석: {max(1, config.COLLECT_INTERVAL_HOURS // 2)}시간마다"
    )

    # 시작 시 즉시 1회 실행
    job_collect()
    job_analyze()

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("스케줄러 종료")
        scheduler.shutdown()


if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    start()

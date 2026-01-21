"""Scheduled AI News Research Agent - runs daily and emails results."""

import asyncio
import logging
import os
import signal
import sys
import warnings
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

# Suppress experimental feature warnings from ADK
warnings.filterwarnings("ignore", message=".*EXPERIMENTAL.*")

# Load environment variables
load_dotenv()

from services import (
    run_research_agent,
    upload_to_drive,
    send_research_email,
    cleanup_old_files,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


async def daily_research_task():
    """
    The main daily research task:
    1. Cleans up previous run files
    2. Runs the research agent
    3. Uploads results to Google Drive
    4. Sends email with results
    """
    logger.info("=" * 50)
    logger.info("Starting daily research task...")
    logger.info("=" * 50)

    try:
        # Step 1: Cleanup previous run files first
        deleted = cleanup_old_files(keep_latest=False)
        if deleted:
            logger.info(f"Cleaned up {len(deleted)} files from previous run")

        # Step 2: Run the research agent
        logger.info("Running research agent...")
        md_file, trace_file = await run_research_agent()
        logger.info(f"Research complete: {md_file.name}")

        # Step 3: Upload to Google Drive
        try:
            drive_file_id = upload_to_drive(md_file)
            logger.info(f"Uploaded to Google Drive: {drive_file_id}")
        except Exception as e:
            logger.error(f"Google Drive upload failed: {e}")

        # Step 4: Send email
        try:
            message_id = send_research_email(md_file)
            logger.info(f"Email sent successfully: {message_id}")
        except Exception as e:
            logger.error(f"Email send failed: {e}")

        logger.info("Daily research task completed!")
        logger.info("=" * 50)

    except Exception as e:
        logger.error(f"Daily research task failed: {e}", exc_info=True)


async def main():
    """Main entry point - sets up scheduler and runs forever."""
    # Get schedule config from env
    schedule_hour = int(os.getenv("SCHEDULE_HOUR", "6"))
    schedule_minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
    timezone = os.getenv("TIMEZONE", "America/Los_Angeles")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        daily_research_task,
        CronTrigger(hour=schedule_hour, minute=schedule_minute, timezone=timezone),
        id="daily_research",
        name="Daily AI News Research",
    )

    scheduler.start()
    logger.info(f"Scheduler started. Daily task scheduled for {schedule_hour:02d}:{schedule_minute:02d} {timezone}")
    logger.info("Press Ctrl+C to exit")

    # Run forever
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    # Allow manual run with --now flag
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        logger.info("Running research task immediately (--now flag)")
        asyncio.run(daily_research_task())
    else:
        asyncio.run(main())

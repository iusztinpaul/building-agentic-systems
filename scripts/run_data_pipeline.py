"""
Run the Substack RSS ETL pipeline.

Usage:
    uv run python scripts/run_data_pipeline.py https://www.decodingai.com/feed
    uv run python scripts/run_data_pipeline.py https://www.decodingai.com/feed https://other.substack.com/feed
"""

import asyncio
import logging
import sys

from twin.config.settings import settings
from twin.data.substack_rss import SubstackRSSFeedETL
from twin.db import init_mongodb

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")


async def main(feed_urls: list[str]) -> None:
    client = await init_mongodb(
        settings.mongo.mongo_uri, settings.mongo.mongo_initdb_database
    )

    try:
        etl = SubstackRSSFeedETL()
        documents = await etl.run_batch(feed_urls)
        print(f"\nDone. Ingested {len(documents)} new documents.")
    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: uv run python scripts/run_data_pipeline.py <feed_url> [feed_url ...]"
        )
        sys.exit(1)

    asyncio.run(main(sys.argv[1:]))

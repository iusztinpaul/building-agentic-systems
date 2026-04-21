"""
Validate the local MongoDB setup supports all three search pillars:
  1. Text Search   ($match on text index)
  2. Vector Search ($vectorSearch via mongot)
  3. Graph Search  ($graphLookup)

Usage:
    uv run python scripts/test_mongodb_setup.py
"""

import asyncio

from beanie import Document, init_beanie
from pydantic import Field
from pymongo import AsyncMongoClient

from tree.config.settings import settings


# --- Document Model ---


class TwinAsset(Document):
    name: str
    description: str
    embedding: list[float] = Field(default_factory=list)
    parent_id: str | None = None

    class Settings:
        name = "test_twin_assets"


# --- Helpers ---

DATABASE_NAME = "test_twin_db"


async def setup() -> AsyncMongoClient:
    client = AsyncMongoClient(settings.mongo.mongo_uri.get_secret_value())
    await init_beanie(database=client[DATABASE_NAME], document_models=[TwinAsset])
    return client


async def seed_data() -> None:
    await TwinAsset.find_all().delete()

    turbine = TwinAsset(
        name="Wind Turbine Alpha",
        description="High-capacity power generator in the North Sector.",
        embedding=[0.1, 0.8, 0.1],
    )
    await turbine.insert()

    blade = TwinAsset(
        name="Turbine Blade B1",
        description="Aerodynamic carbon-fiber component of the turbine.",
        embedding=[0.12, 0.78, 0.15],
        parent_id=str(turbine.id),
    )
    await blade.insert()

    sensor = TwinAsset(
        name="Vibration Sensor S1",
        description="Monitors vibration levels on the blade.",
        embedding=[0.05, 0.3, 0.9],
        parent_id=str(blade.id),
    )
    await sensor.insert()


async def create_text_index() -> None:
    collection = TwinAsset.get_pymongo_collection()
    await collection.create_index(
        [("name", "text"), ("description", "text")],
        name="text_idx",
    )


async def create_vector_search_index() -> None:
    collection = TwinAsset.get_pymongo_collection()

    cursor = await collection.list_search_indexes()
    existing = [idx["name"] async for idx in cursor]
    if "vector_idx" in existing:
        return

    await collection.create_search_index(
        model={
            "name": "vector_idx",
            "type": "vectorSearch",
            "definition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 3,
                        "similarity": "cosine",
                    }
                ]
            },
        }
    )

    # Wait for mongot to sync the index (Community Edition doesn't expose a
    # "queryable" field — the index is ready once it appears and mongot
    # completes its initial sync). We poll until the index shows up, then
    # give mongot a few extra seconds to finish syncing the data.
    print("  Waiting for vector search index to be ready...", end="", flush=True)
    for _ in range(30):
        cursor = await collection.list_search_indexes("vector_idx")
        results = await cursor.to_list()
        if results:
            # Give mongot time to complete initial sync after index creation.
            await asyncio.sleep(3)
            print(" ready.")
            return
        await asyncio.sleep(2)
        print(".", end="", flush=True)

    print(" timed out.")
    raise TimeoutError("Vector search index did not appear in time.")


# --- Test Pillars ---


async def test_text_search() -> bool:
    print("\n[1/3] Text Search ($text)")

    results = await TwinAsset.find({"$text": {"$search": "carbon fiber"}}).to_list()

    if results:
        print(f"  PASS - found: {results[0].name}")
        return True

    print("  FAIL - no results")
    return False


async def test_vector_search() -> bool:
    print("\n[2/3] Vector Search ($vectorSearch)")

    collection = TwinAsset.get_pymongo_collection()
    pipeline = [
        {
            "$vectorSearch": {
                "index": "vector_idx",
                "path": "embedding",
                "queryVector": [0.1, 0.8, 0.1],
                "numCandidates": 10,
                "limit": 2,
            }
        },
        {"$project": {"name": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]

    cursor = await collection.aggregate(pipeline)
    results = await cursor.to_list()

    if results:
        for r in results:
            print(f"  PASS - found: {r['name']} (score: {r['score']:.4f})")
        return True

    print("  FAIL - no results")
    return False


async def test_graph_search() -> bool:
    print("\n[3/3] Graph Search ($graphLookup)")

    collection = TwinAsset.get_pymongo_collection()
    pipeline = [
        {"$match": {"name": "Wind Turbine Alpha"}},
        {
            "$graphLookup": {
                "from": "test_twin_assets",
                "startWith": {"$toString": "$_id"},
                "connectFromField": "_id_str",
                "connectToField": "parent_id",
                "as": "sub_components",
                "depthField": "depth",
            }
        },
        {
            "$addFields": {
                "sub_components": {
                    "$sortArray": {"input": "$sub_components", "sortBy": {"depth": 1}}
                }
            }
        },
    ]

    # $graphLookup needs a computed string field to compare against parent_id.
    # Add a helper field for the traversal.
    await collection.update_many({}, [{"$set": {"_id_str": {"$toString": "$_id"}}}])

    cursor = await collection.aggregate(pipeline)
    results = await cursor.to_list()

    if results and results[0].get("sub_components"):
        components = results[0]["sub_components"]
        for c in components:
            print(f"  PASS - found child: {c['name']} (depth: {c['depth']})")
        return True

    print("  FAIL - no sub-components found")
    return False


# --- Main ---


async def main() -> None:
    print("=" * 50)
    print("MongoDB Stack Validation")
    print("=" * 50)

    client = await setup()

    try:
        print("\nSeeding test data...")
        await seed_data()

        print("Creating indexes...")
        await create_text_index()
        await create_vector_search_index()

        passed = []
        passed.append(await test_text_search())
        passed.append(await test_vector_search())
        passed.append(await test_graph_search())

        print("\n" + "=" * 50)
        print(f"Results: {sum(passed)}/{len(passed)} passed")
        if all(passed):
            print("All search pillars operational.")
        else:
            print("Some tests failed. Check the output above.")
        print("=" * 50)

    finally:
        # Clean up the test database.
        await client.drop_database(DATABASE_NAME)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())

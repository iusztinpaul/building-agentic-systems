"""
Demo script to explore LangChain's MongoDBGraphStore with Gemini.

This is throwaway exploration code to understand:
- How the graph extractor structures entities/relationships
- What the MongoDB collection schema looks like
- How graph-based queries work

Usage:
    uv run python scripts/demo_graphrag.py ingest
    uv run python scripts/demo_graphrag.py visualize
    uv run python scripts/demo_graphrag.py visualize --output graph.html
"""

import json
import logging
import webbrowser
from pathlib import Path

import click
import networkx as nx
from dotenv import load_dotenv
from langchain_core.documents import Document as LCDocument
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mongodb.graphrag.graph import MongoDBGraphStore
from langchain_text_splitters import TokenTextSplitter
from pymongo import MongoClient
from pyvis.network import Network

load_dotenv()

from tree.config.settings import settings  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GRAPH_COLLECTION = "knowledge_graph_demo"


def _mongo_collection():
    client = MongoClient(settings.mongo.mongo_uri.get_secret_value())
    db = client[settings.mongo.mongo_initdb_database]
    return client, db[GRAPH_COLLECTION]


# ---------- Helpers ----------


def get_documents_from_mongodb() -> list[LCDocument]:
    """Read existing documents from the documents collection and convert to LangChain format."""

    client = MongoClient(settings.mongo.mongo_uri.get_secret_value())
    db = client[settings.mongo.mongo_initdb_database]
    collection = db["documents"]

    docs = []
    for doc in collection.find({"content": {"$ne": None}}).limit(5):
        lc_doc = LCDocument(
            page_content=doc["content"],
            metadata={
                "source_uri": doc.get("source_uri", ""),
                "title": doc.get("title", ""),
                "source_type": doc.get("source_type", ""),
            },
        )
        docs.append(lc_doc)

    client.close()
    logger.info("Loaded %d documents from MongoDB", len(docs))
    return docs


def get_sample_documents() -> list[LCDocument]:
    """Fallback sample documents if the documents collection is empty."""

    texts = [
        (
            "Paul Iusztin is a machine learning engineer who writes about MLOps, "
            "LLMOps, and building production AI systems. He is the author of the "
            "Decoding ML newsletter on Substack. Paul has experience building "
            "end-to-end ML pipelines and advocates for clean architecture in ML projects. "
            "He frequently collaborates with Alex Vesa on ML infrastructure topics."
        ),
        (
            "The Decoding ML newsletter covers topics like retrieval-augmented generation (RAG), "
            "fine-tuning large language models, building data pipelines for ML, and deploying "
            "ML models to production. It is published on Substack and has a growing community "
            "of ML practitioners. The newsletter often features hands-on tutorials using tools "
            "like LangChain, MongoDB, and Prefect."
        ),
        (
            "MongoDB Atlas provides vector search capabilities that can be combined with "
            "knowledge graphs for GraphRAG applications. GraphRAG enables relationship-aware "
            "retrieval by structuring data as entities and relationships rather than flat "
            "vector embeddings. LangChain provides integration with MongoDB for building "
            "GraphRAG pipelines through the langchain-mongodb package."
        ),
    ]

    return [LCDocument(page_content=text) for text in texts]


def inspect_collection(collection_name: str) -> None:
    """Print the structure of the graph collection for exploration."""

    client = MongoClient(settings.mongo.mongo_uri.get_secret_value())
    db = client[settings.mongo.mongo_initdb_database]
    collection = db[collection_name]

    count = collection.count_documents({})
    logger.info("Collection '%s' has %d documents", collection_name, count)

    print("\n" + "=" * 80)
    print(f"COLLECTION: {collection_name} ({count} documents)")
    print("=" * 80)

    for i, doc in enumerate(collection.find().limit(10)):
        print(f"\n--- Entity {i + 1} ---")
        doc["_id"] = str(doc["_id"])
        print(json.dumps(doc, indent=2, default=str))

    client.close()


# ---------- CLI ----------


@click.group()
def cli():
    """Demo CLI for exploring LangChain GraphRAG with MongoDB."""


@cli.command()
def ingest():
    """Extract knowledge graph entities from documents and store in MongoDB."""

    # --- 1. Load documents ---
    docs = get_documents_from_mongodb()
    if not docs:
        logger.info("No documents found in MongoDB, using sample documents")
        docs = get_sample_documents()

    # --- 2. Chunk documents ---
    text_splitter = TokenTextSplitter(chunk_size=512, chunk_overlap=64)
    chunked_docs = text_splitter.split_documents(docs)
    chunked_docs = chunked_docs[:10]
    logger.info("Split into %d chunks", len(chunked_docs))
    if chunked_docs:
        logger.info("First chunk:\n%s", chunked_docs[0].page_content)

    # --- 3. Set up LLM for entity extraction ---
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash-lite",
        api_key=settings.google_api_key.get_secret_value(),
        temperature=0,
    )

    # --- 4. Create MongoDBGraphStore ---
    graph_store = MongoDBGraphStore(
        connection_string=settings.mongo.mongo_uri.get_secret_value(),
        database_name=settings.mongo.mongo_initdb_database,
        collection_name=GRAPH_COLLECTION,
        entity_extraction_model=llm,
    )

    # --- 5. Extract entities and build graph ---
    logger.info(
        "Extracting entities from %d chunks (this may take a minute)...",
        len(chunked_docs),
    )
    graph_store.add_documents(chunked_docs)
    logger.info("Graph extraction complete!")

    # --- 6. Inspect what was stored ---
    inspect_collection(GRAPH_COLLECTION)

    # --- 7. Query the graph ---
    queries = [
        "What topics does Paul Iusztin write about?",
        "What tools are used for building ML pipelines?",
    ]

    print("\n" + "=" * 80)
    print("GRAPH QUERIES")
    print("=" * 80)

    for query in queries:
        print(f"\nQ: {query}")
        try:
            response = graph_store.chat_response(query)
            print(f"A: {response.content}")
        except Exception as e:
            logger.error("Query failed: %s", e)

    graph_store.close()


@cli.command()
@click.option(
    "--output",
    "-o",
    default="knowledge_graph.html",
    help="Output HTML file path.",
)
@click.option(
    "--no-open",
    is_flag=True,
    default=False,
    help="Don't open the browser automatically.",
)
def visualize(output: str, no_open: bool):
    """Visualize the knowledge graph stored in MongoDB as an interactive HTML page."""

    client, collection = _mongo_collection()
    docs = list(collection.find())
    client.close()

    if not docs:
        logger.error("No entities found in '%s'. Run 'ingest' first.", GRAPH_COLLECTION)
        raise SystemExit(1)

    logger.info("Building graph from %d entities", len(docs))

    G = nx.DiGraph()

    # Build nodes
    for doc in docs:
        node_id = str(doc["_id"])
        entity_type = doc.get("type", "unknown")
        attrs = doc.get("attributes", {})

        label_parts = [node_id]
        if entity_type:
            label_parts.append(f"[{entity_type}]")

        hover_lines = [f"Type: {entity_type}"]
        for k, v in attrs.items():
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            hover_lines.append(f"{k}: {v}")

        G.add_node(
            node_id,
            label="\n".join(label_parts),
            title="\n".join(hover_lines),
            group=entity_type,
        )

    # Build edges
    for doc in docs:
        source = str(doc["_id"])
        rels = doc.get("relationships", {})
        targets = rels.get("target_ids", [])
        types = rels.get("types", [])
        attrs_list = rels.get("attributes", [])

        for i, target in enumerate(targets):
            target = str(target)
            rel_type = types[i] if i < len(types) else ""
            rel_attrs = attrs_list[i] if i < len(attrs_list) else {}

            hover = f"Relationship: {rel_type}"
            if isinstance(rel_attrs, dict) and rel_attrs:
                for k, v in rel_attrs.items():
                    if isinstance(v, list):
                        v = ", ".join(str(x) for x in v)
                    hover += f"\n{k}: {v}"

            # Add target node if it doesn't exist yet (dangling reference)
            if target not in G:
                G.add_node(target, label=target, title=target, group="unknown")

            G.add_edge(source, target, label=rel_type, title=hover)

    logger.info("Graph: %d nodes, %d edges", G.number_of_nodes(), G.number_of_edges())

    # Render with pyvis
    net = Network(
        height="900px",
        width="100%",
        directed=True,
        bgcolor="#1a1a2e",
        font_color="white",
        cdn_resources="in_line",
    )
    net.from_nx(G)
    net.set_options(
        """
    {
      "interaction": {
        "hover": true,
        "tooltipDelay": 100,
        "navigationButtons": true
      },
      "nodes": {
        "font": {"size": 12, "multi": "html"},
        "shape": "dot",
        "size": 16
      },
      "edges": {
        "font": {"size": 10, "align": "middle"},
        "arrows": {"to": {"enabled": true, "scaleFactor": 0.8}},
        "smooth": {"type": "curvedCW", "roundness": 0.2}
      },
      "physics": {
        "forceAtlas2Based": {
          "gravitationalConstant": -50,
          "centralGravity": 0.01,
          "springLength": 200,
          "springConstant": 0.05,
          "damping": 0.4
        },
        "solver": "forceAtlas2Based",
        "stabilization": {"iterations": 150}
      }
    }
    """
    )

    output_path = Path(output).resolve()
    net.save_graph(str(output_path))
    logger.info("Graph saved to %s", output_path)

    if not no_open:
        webbrowser.open(f"file://{output_path}")


if __name__ == "__main__":
    cli()

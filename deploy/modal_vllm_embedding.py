"""Deploy voyageai/voyage-4-nano as an OpenAI-compatible embedding server on Modal via vLLM."""

import json
import os
import subprocess

import modal

MODEL_NAME = "voyageai/voyage-4-nano"
MODEL_REVISION = "main"

MINUTES = 60
VLLM_PORT = 8000

vllm_image = (
    modal.Image.from_registry("nvidia/cuda:12.8.0-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install(
        "vllm==0.17.1",
        "huggingface-hub>=0.34.0,<1.0",
    )
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})
)

hf_cache_vol = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
vllm_cache_vol = modal.Volume.from_name("vllm-cache", create_if_missing=True)

app = modal.App("vllm-embedding-models")


@app.function(
    name="voyageai-voyage-4-nano",
    image=vllm_image,
    gpu="A10G",
    scaledown_window=15 * MINUTES,
    timeout=10 * MINUTES,
    secrets=[modal.Secret.from_name("vllm-embedding-api-key")],
    volumes={
        "/root/.cache/huggingface": hf_cache_vol,
        "/root/.cache/vllm": vllm_cache_vol,
    },
)
@modal.concurrent(max_inputs=64)
@modal.web_server(port=VLLM_PORT, startup_timeout=10 * MINUTES)
def voyageai_voyage_4_nano():
    api_key = os.environ["MODAL_EMBEDDING_API_KEY"]

    cmd = [
        "vllm",
        "serve",
        MODEL_NAME,
        "--revision",
        MODEL_REVISION,
        "--served-model-name",
        MODEL_NAME,
        "--runner",
        "pooling",
        "--convert",
        "embed",
        "--host",
        "0.0.0.0",
        "--port",
        str(VLLM_PORT),
        "--dtype",
        "bfloat16",
        "--max-model-len",
        "32768",
        "--enforce-eager",
        "--api-key",
        api_key,
        "--pooler-config",
        json.dumps({"pooling_type": "MEAN"}),
        "--hf-overrides",
        json.dumps({"architectures": ["VoyageQwen3BidirectionalEmbedModel"]}),
        "--uvicorn-log-level=info",
    ]

    print("Starting vLLM embedding server:", " ".join(cmd))
    subprocess.Popen(cmd)


@app.local_entrypoint()
async def test():
    import aiohttp

    api_key = os.environ.get("MODAL_EMBEDDING_API_KEY", "")
    if not api_key:
        raise SystemExit(
            "Set MODAL_EMBEDDING_API_KEY env var (same value as the Modal secret) to run this test."
        )

    url = await voyageai_voyage_4_nano.get_web_url.aio()
    headers = {"Authorization": f"Bearer {api_key}"}

    async with aiohttp.ClientSession(base_url=url, headers=headers) as session:
        print(f"Health check: {url}")
        async with session.get(
            "/health", timeout=aiohttp.ClientTimeout(total=5 * MINUTES)
        ) as resp:
            assert resp.status == 200, f"Health check failed: {resp.status}"
        print("Health check passed")

        payload = {
            "model": MODEL_NAME,
            "input": ["Hello, world!", "How are you?"],
        }
        async with session.post("/v1/embeddings", json=payload) as resp:
            result = await resp.json()
            assert result["object"] == "list", f"Unexpected response: {result}"
            for item in result["data"]:
                dim = len(item["embedding"])
                print(f"  index={item['index']} dims={dim}")
        print("Embedding test passed")

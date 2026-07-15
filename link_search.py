from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_MODEL = "qwen3:0.6b-q4_K_M"
MAX_RESULTS = 3


def json_request(url: str, payload: dict, headers: dict[str, str], timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def web_search(query: str, api_key: str) -> list[dict[str, str]]:
    response = json_request(
        "https://ollama.com/api/web_search",
        {"query": query, "max_results": MAX_RESULTS},
        {"Authorization": f"Bearer {api_key}"},
        30,
    )
    results = []
    for result in response.get("results", []):
        if not isinstance(result, dict) or not result.get("url"):
            continue
        results.append(
            {
                "title": str(result.get("title") or "Untitled"),
                "url": str(result["url"]),
                "content": str(result.get("content") or "")[:240],
            }
        )
    return results


def choose_result(query: str, results: list[dict[str, str]], model: str = DEFAULT_MODEL) -> dict[str, str | None]:
    listing = "\n\n".join(
        f"[{index}] {result['title']}\nURL: {result['url']}\nSnippet: {result['content']}"
        for index, result in enumerate(results, start=1)
    )
    prompt = f"""Choose the single result that best matches the user's request.
Return only JSON with string fields named title and url. The URL must be copied
exactly from the results. Return null values if none is relevant.

User request: {query}

Results:
{listing}

/no_think"""
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    response = json_request(
        f"{host}/api/chat",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Select only from the supplied results and return JSON."},
                {"role": "user", "content": prompt},
            ],
            "format": "json",
            "stream": False,
            "think": False,
            "options": {"temperature": 0.2, "num_ctx": 2048, "num_predict": 120},
        },
        {},
        180,
    )
    selection = json.loads(str(response.get("message", {}).get("content", "")))
    selected_url = selection.get("url")
    match = next((result for result in results if result["url"] == selected_url), None)
    if not match:
        return {"title": None, "url": None}
    return {"title": match["title"], "url": match["url"]}


def search(query: str, api_key: str, model: str = DEFAULT_MODEL) -> dict[str, str | None]:
    results = web_search(query, api_key)
    if not results:
        raise RuntimeError("No search results were returned")
    return choose_result(query, results, model)


def main() -> int:
    query = " ".join(sys.argv[1:]).strip()
    api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
    model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    if not query or not api_key:
        print("A query and OLLAMA_API_KEY are required.", file=sys.stderr)
        return 2
    try:
        print(json.dumps(search(query, api_key, model)))
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(f"Link search failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Diagnóstico rápido de la conexión con GitHub Models (se puede borrar)."""
import os, requests
tok = os.environ["GITHUB_TOKEN"]
for modelo in ["openai/gpt-4.1-mini", "openai/gpt-4o-mini"]:
    for rf in (True, False):
        body = {"model": modelo, "messages": [{"role": "user", "content": 'Respondé {"ok": true}'}]}
        if rf:
            body["response_format"] = {"type": "json_object"}
        r = requests.post("https://models.github.ai/inference/chat/completions", json=body, timeout=60,
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
                          allow_redirects=False)
        print(modelo, "json_format" if rf else "plain", r.status_code, dict((k, v) for k, v in r.headers.items() if k.lower() in ("content-type", "location", "x-ratelimit-remaining-requests", "server")), repr(r.text[:200]), flush=True)

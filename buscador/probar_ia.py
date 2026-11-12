"""Diagnóstico rápido de la conexión con GitHub Models (se puede borrar)."""
import os, subprocess, requests
tok = os.environ["GITHUB_TOKEN"]
H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
body = {"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "Decí hola"}]}
print("catalog:", requests.get("https://models.github.ai/catalog/models", headers=H, timeout=30).text[:150], flush=True)
for url, b in [("https://models.github.ai/inference/chat/completions", body),
               ("https://models.inference.ai.azure.com/chat/completions", {**body, "model": "gpt-4o-mini"})]:
    try:
        r = requests.post(url, json=b, headers=H, timeout=60)
        print(url, r.status_code, r.headers.get("content-type"), repr(r.text[:250]), flush=True)
    except Exception as e:
        print(url, "EXC", e, flush=True)
print(subprocess.run(["curl", "-sS", "-i", "https://models.github.ai/inference/chat/completions",
      "-H", f"Authorization: Bearer {tok}", "-H", "Content-Type: application/json",
      "-d", '{"model":"openai/gpt-4o-mini","messages":[{"role":"user","content":"hola"}]}'],
      capture_output=True, text=True).stdout[:900], flush=True)
print("env proxies:", {k: v for k, v in os.environ.items() if "proxy" in k.lower()}, flush=True)

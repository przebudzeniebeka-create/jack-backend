from fastapi import FastAPI

app = FastAPI(title="JackQS API — sanity")

@app.get("/")
def root():
    return {"ok": True, "service": "jackqs-api"}

@app.get("/api/health")
def health():
    return {"ok": True}

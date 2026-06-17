import uvicorn

if __name__ == "__main__":
    # localhost only — this is a personal tool with no auth in front of it
    uvicorn.run("app.api:app", host="127.0.0.1", port=8000, reload=False)

import uvicorn

from config import SERVICE_RESTART_HOST, SERVICE_RESTART_PORT


if __name__ == "__main__":
    uvicorn.run("main:app", host=SERVICE_RESTART_HOST, port=SERVICE_RESTART_PORT)

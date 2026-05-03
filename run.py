"""IRAS development server launcher.

Sets WindowsSelectorEventLoopPolicy before starting uvicorn so that
psycopg async (required by LangGraph's AsyncPostgresSaver) works on Windows.
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run("iras.api.app:app", host="0.0.0.0", port=8000, reload=True)

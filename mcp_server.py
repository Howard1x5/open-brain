"""
MCP Server for Open Brain
Exposes search_brain, list_recent, and add_memory tools via stdio and HTTP transports.
"""

import os
import sys
import json
import logging
import asyncio
from typing import Optional, Any
from datetime import datetime, timedelta

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Valid categories
VALID_CATEGORIES = {"person", "project", "idea", "decision", "admin", "general"}

# Embedding model singleton
_embedding_model = None


def get_embedding_model():
    """Load embedding model as singleton."""
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        model_name = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        logger.info(f"Loading embedding model: {model_name}")
        _embedding_model = SentenceTransformer(model_name)
        logger.info("Embedding model loaded")
    return _embedding_model


def get_db_connection():
    """Get database connection."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise EnvironmentError("DATABASE_URL not set")
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


def generate_embedding(text: str) -> list:
    """Generate embedding vector for text."""
    model = get_embedding_model()
    embedding = model.encode(text, convert_to_numpy=True)
    return embedding.tolist()


# Create MCP server
server = Server("open-brain")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="search_brain",
            description="Search memories using semantic similarity. Returns relevant memories based on meaning, not just keywords.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query text"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5, max 20)",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="list_recent",
            description="List recent memories from the last N days, optionally filtered by category.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back (default 7)",
                        "default": 7
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by category (person, project, idea, decision, admin, general)",
                        "enum": ["person", "project", "idea", "decision", "admin", "general"]
                    }
                }
            }
        ),
        Tool(
            name="add_memory",
            description="Add a new memory to the brain. The text will be classified and embedded automatically.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The memory text to add"
                    }
                },
                "required": ["text"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    try:
        if name == "search_brain":
            return await handle_search_brain(arguments)
        elif name == "list_recent":
            return await handle_list_recent(arguments)
        elif name == "add_memory":
            return await handle_add_memory(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.exception(f"Tool error: {e}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def handle_search_brain(arguments: dict) -> list[TextContent]:
    """Handle search_brain tool calls."""
    query = arguments.get("query", "").strip()
    if not query:
        return [TextContent(type="text", text="Error: query is required and cannot be empty")]
    
    limit = arguments.get("limit", 5)
    limit = max(1, min(20, int(limit)))  # Clamp to 1-20
    
    # Generate query embedding
    try:
        embedding = generate_embedding(query)
    except Exception as e:
        return [TextContent(type="text", text=f"Error generating embedding: {e}")]
    
    # Search database
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, raw_text, category, people, topics, action_item,
                       created_at, confidence, epoch,
                       1 - (embedding <=> %s::vector) as similarity,
                       effective_confidence(confidence, created_at, category) as decayed_confidence
                FROM memories
                WHERE embedding IS NOT NULL
                ORDER BY (1 - (embedding <=> %s::vector)) * effective_confidence(confidence, created_at, category) DESC
                LIMIT %s
            """, (str(embedding), str(embedding), limit))
            
            rows = cur.fetchall()
    finally:
        conn.close()
    
    if not rows:
        return [TextContent(
            type="text", 
            text=f"No memories found matching '{query}'. Try different search terms."
        )]
    
    # Format results
    results = []
    for row in rows:
        date_str = row["created_at"].strftime("%Y-%m-%d") if row["created_at"] else "unknown"
        people_str = ", ".join(row["people"]) if row["people"] else "none"
        topics_str = ", ".join(row["topics"][:3]) if row["topics"] else "none"
        
        epoch_str = row.get("epoch", "unknown")
        result = (
            f"[{row['category']} | {date_str} | {epoch_str}] {row['raw_text']}\n"
            f"Similarity: {row['similarity']:.2f} | Confidence: {row['decayed_confidence']:.2f} | People: {people_str} | Topics: {topics_str}"
        )
        results.append(result)
    
    response = f"Found {len(rows)} memories:\n\n" + "\n\n".join(results)
    return [TextContent(type="text", text=response)]


async def handle_list_recent(arguments: dict) -> list[TextContent]:
    """Handle list_recent tool calls."""
    days = arguments.get("days", 7)
    days = max(1, min(365, int(days)))  # Clamp to 1-365
    
    category = arguments.get("category")
    if category and category not in VALID_CATEGORIES:
        return [TextContent(type="text", text=f"Invalid category. Must be one of: {', '.join(VALID_CATEGORIES)}")]
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if category:
                cur.execute("""
                    SELECT id, raw_text, category, created_at, confidence
                    FROM memories
                    WHERE created_at >= NOW() - INTERVAL '%s days'
                    AND category = %s
                    ORDER BY created_at DESC
                    LIMIT 50
                """, (days, category))
            else:
                cur.execute("""
                    SELECT id, raw_text, category, created_at, confidence
                    FROM memories
                    WHERE created_at >= NOW() - INTERVAL '%s days'
                    ORDER BY created_at DESC
                    LIMIT 50
                """, (days,))
            
            rows = cur.fetchall()
    finally:
        conn.close()
    
    if not rows:
        filter_text = f" in category '{category}'" if category else ""
        return [TextContent(
            type="text", 
            text=f"No memories found from the last {days} days{filter_text}."
        )]
    
    # Format results
    results = []
    for row in rows:
        date_str = row["created_at"].strftime("%Y-%m-%d") if row["created_at"] else "unknown"
        text_preview = row["raw_text"][:100] + ("..." if len(row["raw_text"]) > 100 else "")
        results.append(f"[{date_str}] [{row['category']}] {text_preview}")
    
    filter_text = f" (category: {category})" if category else ""
    response = f"Memories from last {days} days{filter_text}:\n\n" + "\n".join(results)
    return [TextContent(type="text", text=response)]


async def handle_add_memory(arguments: dict) -> list[TextContent]:
    """Handle add_memory tool calls."""
    text = arguments.get("text", "").strip()
    if not text:
        return [TextContent(type="text", text="Error: text is required and cannot be empty")]
    
    # Import and call the capture pipeline
    from capture import capture
    
    try:
        result = await capture(text)
        return [TextContent(type="text", text=result["confirmation_message"])]
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        logger.exception(f"Add memory failed: {e}")
        return [TextContent(type="text", text=f"Error adding memory: {e}")]


async def run_stdio():
    """Run MCP server in stdio mode."""
    logger.info("Starting MCP server in stdio mode...")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def run_http(port: int):
    """Run MCP server in HTTP mode."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route
    import uvicorn
    
    logger.info(f"Starting MCP server in HTTP mode on port {port}...")
    
    sse = SseServerTransport("/messages")
    
    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())
    
    async def handle_messages(request):
        await sse.handle_post_message(request.scope, request.receive, request._send)


    async def handle_api_search(request):
        """REST endpoint for semantic search — POST {"query": "...", "limit": 5}"""
        from starlette.responses import JSONResponse
        try:
            body = await request.json()
            query = body.get("query", "").strip()
            if not query:
                return JSONResponse({"error": "query is required"}, status_code=400)

            limit = min(20, max(1, int(body.get("limit", 5))))

            embedding = generate_embedding(query)

            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, raw_text, category, summary, created_at, confidence,
                               1 - (embedding <=> %s::vector) as similarity,
                               effective_confidence(confidence, created_at, category) as decayed_confidence
                        FROM memories
                        WHERE embedding IS NOT NULL
                        ORDER BY (1 - (embedding <=> %s::vector)) * effective_confidence(confidence, created_at, category) DESC
                        LIMIT %s
                    """, (str(embedding), str(embedding), limit))
                    rows = cur.fetchall()
            finally:
                conn.close()

            results = []
            for row in rows:
                results.append({
                    "id": row["id"],
                    "summary": row["summary"],
                    "category": row["category"],
                    "raw_text": row["raw_text"][:400],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "similarity": round(row["similarity"], 3),
                    "confidence": round(row["decayed_confidence"], 3),
                })

            return JSONResponse({"results": results, "count": len(results)})
        except Exception as e:
            logger.exception(f"API search error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_api_add(request):
        """Simple REST endpoint for hooks — POST {"text": "..."}"""
        from starlette.responses import JSONResponse
        try:
            body = await request.json()
            text = body.get("text", "").strip()
            if not text:
                return JSONResponse({"error": "text is required"}, status_code=400)
            from capture import capture
            result = await capture(text)
            return JSONResponse({
                "success": result.get("success", False),
                "memory_id": result.get("memory_id"),
                "category": result.get("category"),
                "summary": result.get("summary"),
            })
        except Exception as e:
            logger.exception(f"API add error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    starlette_app = Starlette(
        routes=[
            Route("/sse", handle_sse),
            Route("/messages", handle_messages, methods=["POST"]),
            Route("/api/search", handle_api_search, methods=["POST"]),
            Route("/api/add", handle_api_add, methods=["POST"]),
        ]
    )
    
    config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
    server_instance = uvicorn.Server(config)
    await server_instance.serve()


def main():
    """Main entry point."""
    # Parse command line arguments
    mode = "stdio"  # Default
    
    if "--http" in sys.argv:
        mode = "http"
    elif "--stdio" in sys.argv:
        mode = "stdio"
    
    # Preload embedding model
    logger.info("Preloading embedding model...")
    try:
        get_embedding_model()
    except Exception as e:
        logger.error(f"Failed to load embedding model: {e}")
        sys.exit(1)
    
    # Run server
    if mode == "stdio":
        asyncio.run(run_stdio())
    else:
        port = int(os.getenv("MCP_HTTP_PORT", "8765"))
        try:
            asyncio.run(run_http(port))
        except OSError as e:
            if "Address already in use" in str(e):
                logger.error(f"Port {port} is already in use. Check if another instance is running: lsof -i :{port}")
            raise


if __name__ == "__main__":
    main()

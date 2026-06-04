import os
import re
import json
import requests
import mysql.connector
from mysql.connector import pooling
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv
from db import DB_connection
# ─── Load Environment Variables ───────────────────────────────────────────────
load_dotenv()

app = FastAPI(title="E-Commerce SQL Chatbot", version="1.0.0")

# ─── CORS Middleware ──────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── MySQL Connection Pool (lazy — server starts even if DB is temporarily down) ─
_db_pool = None


def get_db_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = pooling.MySQLConnectionPool(
            pool_name="ecommerce_pool",
            pool_size=5,
            host=os.getenv("DB_HOST", "localhost"),
            user=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASS", ""),
            database=os.getenv("DB_NAME", "fakedb"),
            port=int(os.getenv("DB_PORT", 3306)),
            connection_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
        )
    return _db_pool


def run_select_query(sql: str) -> str:
    """Run a SELECT and return context text for the reply model."""
    conn = get_db_pool().get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql)
        rows = cursor.fetchall()
        cursor.close()
        if not rows:
            return "INVENTORY_EMPTY: No products matched the customer's question."
        return f"INVENTORY_DATA:\n{json.dumps(rows, indent=2, default=str)}"
    finally:
        conn.close()


def build_reply_system_prompt(db_context: str) -> str:
    base = (
        "You are a friendly e-commerce customer support assistant.\n"
        "Answer the customer's question warmly and concisely in 2-3 sentences.\n"
        "Never mention SQL, databases, queries, or technical details. Speak naturally.\n\n"
    )
    if not db_context:
        return base + "Answer from general knowledge (returns, shipping, policies, etc)."

    if db_context.startswith("INVENTORY_EMPTY"):
        return (
            base
            + "The catalog has no products matching what they asked for. "
            "Tell them clearly we do not carry that item right now. "
            "Offer one brief alternative (e.g. browse other products). "
            "Do not say systems are down or ask for style/size to avoid answering.\n"
        )

    if db_context.startswith("INVENTORY_ERROR"):
        return (
            base
            + "Inventory lookup failed. Apologize once and ask them to try again shortly. "
            "Do not invent stock, pretend to search, or ask for style/size as a workaround.\n"
        )

    return base + f"Use this catalog data to answer accurately:\n{db_context}\n"

# ─── Pydantic Models (Request / Response) ────────────────────────────────────
class Message(BaseModel):
    role: str       # 'user' or 'assistant'
    content: str

class ChatRequest(BaseModel):
    message: str
    history: Optional[List[Message]] = []

class ChatResponse(BaseModel):
    reply: str
    error: Optional[bool] = None

# ─── AI Helper (Groq) ─────────────────────────────────────────────────────────
def get_api_key() -> str:
    key = os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise Exception(
            "Missing GROQ_API_KEY in .env (get a free key at https://console.groq.com)"
        )
    return key


def ask_ai(input_messages: list) -> str:
    """
    Calls the Groq API.
    Accepts a list of message dicts.
    Uses Few-Shot prompting and Role Separation for best accuracy.
    """
    url = "https://api.groq.com/openai/v1/chat/completions"

    response = requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {get_api_key()}"
        },
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": input_messages,
            "temperature": 0.1
        },
        timeout=30
    )

    data = response.json()

    if "error" in data:
        raise Exception(data["error"].get("message", "Groq API error"))

    if not data.get("choices"):
        raise Exception("No response from Groq")

    return data["choices"][0]["message"]["content"].strip()


# ─── Serve Frontend ───────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/health")
def health():
    return {"status": "ok", "port": int(os.getenv("PORT", 3002))}


@app.get("/db-check")
def db_check():
    """Verify MySQL is reachable and the products table exists."""
    try:
        conn = get_db_pool().get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM products")
        count = cursor.fetchone()[0]
        cursor.close()
        conn.close()
        return {
            "ok": True,
            "database": os.getenv("DB_NAME", "fakedb"),
            "host": os.getenv("DB_HOST", "localhost"),
            "product_count": count,
        }
    except Exception as e:
        return {
            "ok": False,
            "database": os.getenv("DB_NAME", "fakedb"),
            "host": os.getenv("DB_HOST", "localhost"),
            "error": str(e),
        }

@app.get("/user")
def user():
    try:
        connection = DB_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM users")
        users = cursor.fetchall()
        cursor.close()
        connection.close()
        return users
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── Chat Endpoint ────────────────────────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest):
    message = body.message.strip()
    history = body.history or []

    if not message:
        raise HTTPException(status_code=400, detail="No message provided")

    try:
        # ── Step 1: SQL Router ─────────────────────────────────────────────────
        # Prompt Engineering: System Role + Few-Shot Examples + Schema Definitions
        router_messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert SQL assistant for an e-commerce platform.\n"
                    "Your task is to analyze user queries and output ONLY a valid MySQL SELECT statement, "
                    "OR the exact word 'NODB' if no database lookup is needed.\n\n"
                    "Database Schema:\n"
                    "1. products (id INT, name VARCHAR, price DECIMAL, stock INT, image_url VARCHAR)\n"
                    "2. orders (id INT, user_id INT, created_at DATETIME)\n"
                    "3. order_items (id INT, order_id INT, product_id INT, quantity INT)\n"
                    "4. users (id INT, name VARCHAR, email VARCHAR, role VARCHAR)\n\n"
                    "Rules:\n"
                    "- For product/order/user inquiries, output ONLY the raw SQL query.\n"
                    "- Always use LIMIT 20.\n"
                    "- Do not use markdown backticks or explanations.\n"
                    "- If the question is general (greetings, policies), output NODB."
                )
            },
            # Few-Shot Examples
            {"role": "user",      "content": "Show me all products"},
            {"role": "assistant", "content": "SELECT * FROM products LIMIT 20"},
            {"role": "user",      "content": "What is your return policy?"},
            {"role": "assistant", "content": "NODB"},
            {"role": "user",      "content": "Which products are out of stock?"},
            {"role": "assistant", "content": "SELECT * FROM products WHERE stock = 0 LIMIT 20"},
            {"role": "user",      "content": "Do you have blue shirts?"},
            {"role": "assistant", "content": "SELECT * FROM products WHERE name LIKE '%blue%' AND name LIKE '%shirt%' LIMIT 20"},
            {"role": "user",      "content": "How many orders has user 3 made?"},
            {"role": "assistant", "content": "SELECT COUNT(*) as total_orders FROM orders WHERE user_id = 3 LIMIT 20"},
            # Actual user message
            {"role": "user", "content": message}
        ]

        intent = ask_ai(router_messages)

        # Clean up any markdown backticks the model might add
        clean_intent = re.sub(r"```sql", "", intent, flags=re.IGNORECASE)
        clean_intent = re.sub(r"```", "", clean_intent).strip()

        db_context = ""

        # ── Step 2: Run SQL against the database ───────────────────────────────
        if clean_intent.upper().startswith("SELECT"):
            try:
                db_context = run_select_query(clean_intent)
            except Exception as db_err:
                print(f"DB error: {db_err}")
                db_context = f"INVENTORY_ERROR: {db_err}"

        system_content = build_reply_system_prompt(db_context)

        reply_messages = [{"role": "system", "content": system_content}]

        # Inject conversation history for context awareness
        for h in history:
            reply_messages.append({"role": h.role, "content": h.content})

        reply_messages.append({"role": "user", "content": message})

        reply = ask_ai(reply_messages)
        return ChatResponse(reply=reply)

    except Exception as err:
        print(f"Chat error: {err}")
        raise HTTPException(status_code=500, detail=str(err))


# ─── Start Server ─────────────────────────────────────────────────────────────
def log_db_status():
    check = db_check()
    if check.get("ok"):
        print(
            f"Database OK: {check['host']}/{check['database']} "
            f"({check['product_count']} products)"
        )
    else:
        print(
            f"Database NOT ready: {check.get('error')}\n"
            f"  Run: python setup_database.py\n"
            f"  Or:  mysql -u root -p < dump.sql"
        )


if __name__ == "__main__":
    import subprocess, sys
    port = int(os.getenv("PORT", 3002))
    log_db_status()
    print(f"Chatbot server running at http://localhost:{port}")
    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "app:app",
        "--host", "0.0.0.0",
        "--port", str(port),
        "--reload"
    ])

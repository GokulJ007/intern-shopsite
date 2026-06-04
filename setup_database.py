"""
Create fakedb and load dump.sql using credentials from .env.
Run once: python setup_database.py
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
DUMP = ROOT / "dump.sql"


def read_dump_text() -> str:
    data = DUMP.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    # Windows mysqldump sometimes saves UTF-16 LE without BOM (e.g. starts with -\x00-\x00)
    if len(data) >= 4 and data[1] == 0 and data[3] == 0:
        return data.decode("utf-16-le")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    return data.decode("utf-8")


def connect(**extra):
    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASS", ""),
        port=int(os.getenv("DB_PORT", 3306)),
        charset="utf8mb4",
        **extra,
    )


def _statements_from_dump(sql: str) -> list[str]:
    lines = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    sql = "\n".join(lines)
    sql = re.sub(r"/\*!\d+.*?\*/;?", "", sql, flags=re.DOTALL)
    allowed = re.compile(
        r"^(CREATE DATABASE|CREATE TABLE|DROP TABLE|INSERT INTO|LOCK TABLES|UNLOCK TABLES|USE )",
        re.IGNORECASE,
    )
    parts = []
    for chunk in sql.split(";"):
        stmt = chunk.strip()
        if stmt and allowed.match(stmt):
            parts.append(stmt)
    return parts


def import_with_pymysql():
    statements = _statements_from_dump(read_dump_text())

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for stmt in statements:
                cur.execute(stmt)
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    finally:
        conn.close()


def import_with_mysql_cli():
    host = os.getenv("DB_HOST", "localhost")
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASS", "")
    port = os.getenv("DB_PORT", "3306")
    env = os.environ.copy()
    if password:
        env["MYSQL_PWD"] = password
    return subprocess.run(
            ["mysql", f"-h{host}", f"-P{port}", f"-u{user}"],
            input=read_dump_text(),
            env=env,
            capture_output=True,
            text=True,
        )


def main():
    if not DUMP.is_file():
        print(f"Missing {DUMP}")
        sys.exit(1)

    print(f"Importing {DUMP.name}...")
    try:
        result = import_with_mysql_cli()
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
    except (FileNotFoundError, RuntimeError) as err:
        print(f"mysql CLI failed ({err}); using pymysql...")
        import_with_pymysql()

    conn = connect(database=os.getenv("DB_NAME", "fakedb"))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM products")
            count = cur.fetchone()[0]
    finally:
        conn.close()

    print(f"Done. products table has {count} rows.")
    print("Start the chatbot: python app.py")


if __name__ == "__main__":
    main()

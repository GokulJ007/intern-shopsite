import os
import pymysql
import pymysql.cursors
from dotenv import load_dotenv

load_dotenv()


def DB_connection():
    try:
        return pymysql.connect(
            host=os.getenv("DB_HOST", "localhost"),
            user=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASS", ""),
            database=os.getenv("DB_NAME", "fakedb"),
            port=int(os.getenv("DB_PORT", 3306)),
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.MySQLError as e:
        raise Exception("error in connecting to db") from e

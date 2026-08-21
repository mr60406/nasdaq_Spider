import os
import random
import time

import pymysql
import requests
from bs4 import BeautifulSoup

MAX_PAGES = 3
API_URL = "https://www.nasdaq.com/api/reference-group/paginated/article/664546?page={page}&limit=10"
DB_CONFIG = {
    "host": os.getenv("MARIADB_HOST", "127.0.0.1"),
    "port": int(os.getenv("MARIADB_PORT", "3306")),
    "user": os.getenv("MARIADB_USER", "root"),
    "password": os.getenv("MARIADB_PASSWORD", "1234"),
    "database": os.getenv("MARIADB_DATABASE", "api_db"),
    "charset": "utf8mb4",
    "autocommit": False,
}
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
}


def get_connection():
    return pymysql.connect(**DB_CONFIG)


def load_stock_list(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `股票代碼` FROM `股票基本資料` "
            "WHERE `股票代碼` IS NOT NULL ORDER BY `股票代碼`"
        )
        return [
            str(row[0]).strip().upper()
            for row in cursor.fetchall()
            if row[0] and str(row[0]).strip()
        ]


def create_news_tables(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS `nasdaq_news` (
                `news_id` VARCHAR(64) NOT NULL,
                `title` TEXT,
                `url` TEXT,
                `published_date` VARCHAR(100),
                `published_timestamp` VARCHAR(64),
                `publisher` VARCHAR(255),
                `topic` VARCHAR(255),
                `description` TEXT,
                `full_text` LONGTEXT NOT NULL,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (`news_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS `nasdaq_news_symbols` (
                `news_id` VARCHAR(64) NOT NULL,
                `symbol` VARCHAR(10) NOT NULL,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (`news_id`, `symbol`),
                CONSTRAINT `fk_nasdaq_news_symbols_news`
                    FOREIGN KEY (`news_id`) REFERENCES `nasdaq_news` (`news_id`)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
    connection.commit()


def article_parser(url):
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    article = soup.find("article")
    if article is None:
        raise ValueError("找不到 article")
    content = article.select_one(".field-body")
    if content is None:
        raise ValueError("找不到 .field-body")

    paragraphs = []
    stop_markers = ["On the date of publication,", "More news from"]
    for p in content.find_all("p"):
        text = p.get_text(" ", strip=True)
        if not text:
            continue
        if any(text.startswith(marker) for marker in stop_markers):
            break
        paragraphs.append(text)

    clean_text = "\n\n".join(paragraphs)
    if not clean_text:
        raise ValueError("正文內容為空")
    return clean_text


def normalize_symbols(symbols):
    normalized = []
    for symbol in symbols or []:
        if isinstance(symbol, dict):
            symbol = symbol.get("symbol") or symbol.get("ticker")
        if symbol and str(symbol).strip():
            normalized.append(str(symbol).strip().upper())
    return normalized


def parse_api_item(item):
    link = item.get("node_url")
    if link and link.startswith("/"):
        link = "https://www.nasdaq.com" + link
    return {
        "news_id": item.get("node_id"),
        "title": item.get("node_title"),
        "url": link,
        "date": item.get("created_formatted"),
        "timestamp": item.get("created"),
        "publisher": item.get("publisher_name"),
        "topic": item.get("primary_topic"),
        "symbols": normalize_symbols(item.get("symbolsInTheStory", [])),
        "description": item.get("description"),
    }


def save_news(connection, news):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT IGNORE INTO `nasdaq_news` (
                `news_id`, `title`, `url`, `published_date`,
                `published_timestamp`, `publisher`, `topic`,
                `description`, `full_text`
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(news["news_id"]), news["title"], news["url"], news["date"],
                str(news["timestamp"]) if news["timestamp"] is not None else None,
                news["publisher"], news["topic"], news["description"],
                news["full_text"],
            ),
        )
        news_inserted = cursor.rowcount
        relation_inserted = 0
        for symbol in news["matched_symbols"]:
            cursor.execute(
                "INSERT IGNORE INTO `nasdaq_news_symbols` (`news_id`, `symbol`) VALUES (%s, %s)",
                (str(news["news_id"]), symbol),
            )
            relation_inserted += cursor.rowcount
    connection.commit()
    return news_inserted, relation_inserted


def print_error(stage, news, error):
    print(f"[{stage} 失敗]")
    print("news_id:", news.get("news_id"))
    print("title:", news.get("title"))
    print("url:", news.get("url"))
    print("error:", error)


def main():
    started_at = time.perf_counter()
    stats = {
        "api_news": 0, "matched": 0, "skipped": 0,
        "parser_success": 0, "parser_failed": 0,
        "news_inserted": 0, "news_existing": 0,
        "relations_inserted": 0,
    }
    stock_list = []
    scanned_pages = 0

    connection = get_connection()
    try:
        stock_list = load_stock_list(connection)
        stock_set = set(stock_list)
        print(f"SQL 股票數量: {len(stock_list)}")
        print(f"SQL 股票: {stock_list}")
        create_news_tables(connection)

        for page in range(1, MAX_PAGES + 1):
            print(f"\n掃描 Nasdaq API 第 {page}/{MAX_PAGES} 頁")
            try:
                response = requests.get(API_URL.format(page=page), headers=HEADERS, timeout=20)
                response.raise_for_status()
                items = response.json().get("items", [])
                scanned_pages += 1
            except (requests.RequestException, ValueError) as error:
                print(f"[API 第 {page} 頁失敗] error: {error}")
                continue

            stats["api_news"] += len(items)
            for item in items:
                news = parse_api_item(item)
                matched_symbols = list(dict.fromkeys(
                    symbol for symbol in news["symbols"] if symbol in stock_set
                ))
                if not matched_symbols:
                    stats["skipped"] += 1
                    continue

                stats["matched"] += 1
                news["matched_symbols"] = matched_symbols
                if not news["news_id"] or not news["url"]:
                    stats["parser_failed"] += 1
                    print_error("資料不完整", news, "缺少 news_id 或 url")
                    continue

                delay = random.uniform(2, 3.5)
                print(f"抓取正文: {news['news_id']} {matched_symbols}（等待 {delay:.2f} 秒）")
                time.sleep(delay)
                try:
                    news["full_text"] = article_parser(news["url"])
                    stats["parser_success"] += 1
                except (requests.RequestException, ValueError) as error:
                    stats["parser_failed"] += 1
                    print_error("Article Parser", news, error)
                    continue

                try:
                    inserted, relations = save_news(connection, news)
                    stats["news_inserted"] += inserted
                    stats["news_existing"] += int(inserted == 0)
                    stats["relations_inserted"] += relations
                except pymysql.MySQLError as error:
                    connection.rollback()
                    print_error("MariaDB 寫入", news, error)
    finally:
        connection.close()

    elapsed = time.perf_counter() - started_at
    print("\n" + "=" * 60)
    print("Nasdaq Spider 完成")
    print("=" * 60)
    print(f"SQL 股票數量: {len(stock_list)}")
    print(f"掃描頁數: {scanned_pages}")
    print(f"API 新聞總數: {stats['api_news']}")
    print(f"符合 STOCK_LIST 新聞數: {stats['matched']}")
    print(f"跳過新聞數: {stats['skipped']}")
    print(f"Article Parser 成功數: {stats['parser_success']}")
    print(f"Article Parser 失敗數: {stats['parser_failed']}")
    print(f"新 INSERT 新聞數: {stats['news_inserted']}")
    print(f"已存在新聞數: {stats['news_existing']}")
    print(f"新聞股票關聯 INSERT 數: {stats['relations_inserted']}")
    print(f"總執行時間: {elapsed:.1f} 秒")


if __name__ == "__main__":
    main()

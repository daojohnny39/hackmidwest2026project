import argparse
import json

from .client import MockHTTPError, MockInternetClient
from .db import Corpus, DEFAULT_DB, initialize
from .server import create_server


def demo():
    client = MockInternetClient()
    hits = client.search('"Alex Morgan"', num=100)["organic_results"]
    print(f"Text search: {len(hits)} fictional results")
    unique = {}
    total = 0
    for fixture in ("alex-front", "alex-left", "alex-right"):
        results = client.reverse_image(fixture)["image_results"]
        total += len(results)
        unique.update((result["link"], result) for result in results)
        print(f"Image fixture {fixture}: {len(results)} results")
    print(f"Combined: {total} hits, {len(unique)} unique URLs")
    scraped, errors, links = 0, [], 0
    for url in unique:
        try:
            page = client.fetch(url)
            scraped += 1
            links += len(page["links"])
        except MockHTTPError as exc:
            errors.append({"status": exc.status, "url": url})
    print(json.dumps({"scraped_pages": scraped, "discovered_links": links,
                      "expected_page_failures": errors, "external_requests": client.stats()["external_requests"]}, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Local mock internet: fictional pages, search, and SQLite.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Seed if needed and start the HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--db", default=str(DEFAULT_DB))
    seed = commands.add_parser("seed", help="Create the database (preserves existing data unless --reset)")
    seed.add_argument("--db", default=str(DEFAULT_DB))
    seed.add_argument("--reset", action="store_true", help="Replace mock database contents with the seed world")
    commands.add_parser("demo", help="Search and scrape the running server without paid services")
    args = parser.parse_args()
    if args.command == "seed":
        initialize(args.db, reset=args.reset)
        print(json.dumps(Corpus(args.db).stats(), indent=2))
    elif args.command == "demo":
        demo()
    else:
        server = create_server(args.host, args.port, args.db)
        print(f"Mock internet: {server.base_url}\nSQLite database: {args.db}\nCtrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
